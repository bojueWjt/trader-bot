from __future__ import annotations

import errno
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_ROOT))

import account_a_live_trade_executor as executor

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
LIVE_ADAPTER_BYTES = b"#!/bin/sh\nexit 0\n"
LIVE_ADAPTER_SHA256 = hashlib.sha256(LIVE_ADAPTER_BYTES).hexdigest()
PINNED_REVIEWER_PUBLIC_KEY = b"""-----BEGIN PUBLIC KEY-----
MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEA08wadHyjJ2AenrOJY8aw
m0DHvWF69RlHWHyZNd890fK6ezMdASWlok8H2+1y4E56HMXkEJt74g3OZ+ZNyEtg
GtcLlW04wqyTr//k9s1hj9VSJ5XF7Y8owQ6uAr4t2mhauS+NzRmThzTGdeRmW1qh
HQDCyTQmV6exJLy9BHxn4zg0GBkmxtgQl+kva4Z1px5yoQwzMA0W+Ki/6DQcV1sD
w7EY6M4Dj/DcFl5o/l1Rc2zIaGzbqgJmUJnoxwDbvTlOOhji3w20ASYlSkMlyGRC
3PJCDCdwFYrE/hdlneq/50e+CozNMgO5iU166az5Ca9QEz5VDwUcriTfQj60HN1r
huJetUYXiiF97JcDW+XCvhyVe9CMcYJL+8EoD9G6Us3sM3e05eTnBzvImh8Rj3iG
h94XBrLHKOGSBcD8ATEXn6I3zld0rgNLKWT5AJJYXPadq1SvRj38jjNa822oIolI
7eQT2RsawujPiYVpmOOShQgmNo9+tTLXTtVotf05WEWB+ZayZPQIMN4bu49XF4DY
IEealeEfhnk+Tt5r8R5lkTr/E2sFkHQTKRV0iF0+tW4ppUv48EQEWQdYfF7Qanio
sDvps4uijfJx5w53rrl0Mr1zRLPtnmUHNqNwNMd/iVxj0pcTVZTwI8gNwUkZk4y7
g1QZyXEglG5AJpMyoy91G6kCAwEAAQ==
-----END PUBLIC KEY-----
"""


class AlwaysHeldOperationLock:
    is_held = True

    def require_held(self) -> None:
        return


class InjectedCrash(BaseException):
    pass


class CrashBeforePublishEvidenceWriter(executor.AtomicEvidenceWriter):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.crash_pending = True

    def commit_prepared(
        self,
        evidence: Mapping[str, Any],
        *,
        evidence_sha256: str,
        allow_existing: bool,
    ) -> str:
        if self.crash_pending:
            self.crash_pending = False
            raise InjectedCrash("crash before evidence publication")
        return super().commit_prepared(
            evidence,
            evidence_sha256=evidence_sha256,
            allow_existing=allow_existing,
        )


class LockCheckingEvidenceWriter(executor.AtomicEvidenceWriter):
    def __init__(
        self,
        path: Path,
        operation_lock: executor.LiveOperationLock,
    ) -> None:
        super().__init__(path)
        self.operation_lock = operation_lock

    def prepare(
        self,
        evidence: Mapping[str, Any],
    ) -> tuple[dict[str, Any], str]:
        self.operation_lock.require_held()
        return super().prepare(evidence)

    def commit_prepared(
        self,
        evidence: Mapping[str, Any],
        *,
        evidence_sha256: str,
        allow_existing: bool,
    ) -> str:
        self.operation_lock.require_held()
        return super().commit_prepared(
            evidence,
            evidence_sha256=evidence_sha256,
            allow_existing=allow_existing,
        )


class MutableClock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


class MutableMonotonic:
    def __init__(self, current: float = 0.0) -> None:
        self.current = current

    def __call__(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


class FakeSignatureVerifier:
    def __init__(self) -> None:
        self.payload_hashes: list[str] = []

    def verify(
        self,
        *,
        public_key: bytes,
        payload: bytes,
        signature: bytes,
    ) -> None:
        assert public_key == PINNED_REVIEWER_PUBLIC_KEY
        expected_signature = hashlib.sha256(payload).hexdigest().encode(
            "ascii"
        )
        assert signature == expected_signature
        self.payload_hashes.append(
            hashlib.sha256(payload).hexdigest()
        )


class FakeAdapter:
    def __init__(
        self,
        authorization: executor.CanaryAuthorization,
        *,
        fail_observe: bool = False,
        observation_loss: str = "0.10",
        observation_price: str = "100",
        observation_status: str = "FILLED",
        observation_times: Mapping[str, datetime] | None = None,
        signal_number: int | None = None,
        position_failures: int = 0,
        close_failures: int = 0,
        halt_failures: int = 0,
        position_source: str = executor.EXCHANGE_EVIDENCE_SOURCE,
        position_fetched_at: datetime = NOW,
        final_source: str = executor.EXCHANGE_EVIDENCE_SOURCE,
        final_fetched_at: datetime = NOW,
        halt_source: str = executor.NODE_STATE_EVIDENCE_SOURCE,
        halt_state: str = "HALTED",
        halt_observed_at: datetime = NOW,
        on_resume: Callable[[], None] | None = None,
        resume_failures: int = 0,
        observe_failures: int = 0,
        open_error_after_effect: BaseException | None = None,
        close_error_after_effect: BaseException | None = None,
        final_failures: int = 0,
        post_halt_final_failures: int = 0,
        post_halt_final_fetched_at: datetime | None = None,
        operation_lock: executor.LiveOperationLock | None = None,
    ) -> None:
        self.authorization = authorization
        self.fail_observe = fail_observe
        self.observation_loss = observation_loss
        self.observation_price = observation_price
        self.observation_status = observation_status
        self.observation_times = dict(observation_times or {})
        self.signal_number = signal_number
        self.position_failures = position_failures
        self.close_failures = close_failures
        self.halt_failures = halt_failures
        self.position_source = position_source
        self.position_fetched_at = position_fetched_at
        self.final_source = final_source
        self.final_fetched_at = final_fetched_at
        self.halt_source = halt_source
        self.halt_state = halt_state
        self.halt_observed_at = halt_observed_at
        self.on_resume = on_resume
        self.resume_failures = resume_failures
        self.observe_failures = observe_failures
        self.open_error_after_effect = open_error_after_effect
        self.close_error_after_effect = close_error_after_effect
        self.final_failures = final_failures
        self.post_halt_final_failures = post_halt_final_failures
        self.post_halt_final_fetched_at = post_halt_final_fetched_at
        self.operation_lock = operation_lock
        self.calls: list[str] = []
        self.requests: dict[str, list[dict[str, Any]]] = {}
        self.close_requests: list[Mapping[str, Any]] = []
        self.position_quantity = Decimal(0)
        self.position_side = "FLAT"

    def resume(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self._require_operation_lock()
        self._record_call("resume", request)
        if self.calls.count("resume") <= self.resume_failures:
            raise TimeoutError("injected RESUME response timeout")
        if self.on_resume is not None:
            self.on_resume()
        return self._ack(
            "resume",
            side_effect_id=request["side_effect_id"],
        )

    def submit_open(
        self,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self._require_operation_lock()
        self._record_call("open", request)
        self.position_quantity = self.authorization.quantity
        self.position_side = "LONG"
        if self.authorization.open_side == "SELL":
            self.position_side = "SHORT"
        error = self.open_error_after_effect
        if error is not None:
            self.open_error_after_effect = None
            raise error
        return self._ack(
            "open",
            client_order_id=(
                self.authorization.open_client_order_id
            ),
            side_effect_id=request["side_effect_id"],
        )

    def observe(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self._require_operation_lock()
        self._record_call("observe", request)
        if self.calls.count("observe") <= self.observe_failures:
            raise TimeoutError("injected OBSERVE response timeout")
        signal_number = self.signal_number
        if signal_number is not None:
            self.signal_number = None
            signal.raise_signal(signal_number)
        if self.fail_observe:
            raise RuntimeError("injected observation failure")
        observed_at = self.observation_times.get("observed_at", NOW)
        mark_at = self.observation_times.get("mark_at", NOW)
        loss_monitor_at = self.observation_times.get(
            "loss_monitor_at",
            NOW,
        )
        payload = self._identity()
        payload.update(
            {
                "open_client_order_id": (
                    self.authorization.open_client_order_id
                ),
                "open_status": self.observation_status,
                "filled_quantity": str(
                    self.authorization.quantity
                ),
                "average_fill_price_usdt": self.observation_price,
                "cumulative_net_loss_usdt": self.observation_loss,
                "mark_fresh": True,
                "loss_monitor_healthy": True,
                "observed_at": observed_at.isoformat(),
                "mark_at": mark_at.isoformat(),
                "loss_monitor_at": loss_monitor_at.isoformat(),
                "evidence_sha256": _digest("observe"),
            }
        )
        return payload

    def cancel_open(
        self,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self._require_operation_lock()
        self._record_call("cancel-open", request)
        return self._ack(
            "cancel-open",
            client_order_id=(
                self.authorization.open_client_order_id
            ),
            side_effect_id=request["side_effect_id"],
        )

    def current_position(
        self,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self._require_operation_lock()
        self._record_call("position", request)
        if self.calls.count("position") <= self.position_failures:
            raise RuntimeError("injected position query failure")
        payload = self._identity()
        payload.update(
            {
                "position_side": self.position_side,
                "position_quantity": str(self.position_quantity),
                "source": self.position_source,
                "fetched_at": self.position_fetched_at.isoformat(),
                "evidence_sha256": _digest("position"),
            }
        )
        return payload

    def submit_close(
        self,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self._require_operation_lock()
        self._record_call("close", request)
        self.close_requests.append(dict(request))
        if self.calls.count("close") <= self.close_failures:
            raise RuntimeError("injected close failure")
        filled_quantity = str(request["quantity"])
        self.position_quantity = Decimal(0)
        self.position_side = "FLAT"
        error = self.close_error_after_effect
        if error is not None:
            self.close_error_after_effect = None
            raise error
        return self._ack(
            "close",
            client_order_id=(
                self.authorization.close_client_order_id
            ),
            filled_quantity=filled_quantity,
            intent_id=self.authorization.close_intent_id,
            side_effect_id=request["side_effect_id"],
        )

    def final_snapshot(
        self,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self._require_operation_lock()
        is_post_halt = request.get("phase") == "post-halt-final"
        self._record_call("final-snapshot", request)
        final_call_count = len(self.requests["final-snapshot"])
        if is_post_halt:
            post_halt_count = sum(
                item.get("phase") == "post-halt-final"
                for item in self.requests["final-snapshot"]
            )
            if post_halt_count <= self.post_halt_final_failures:
                raise TimeoutError(
                    "injected post-HALT final snapshot timeout"
                )
        elif final_call_count <= self.final_failures:
            raise TimeoutError("injected final snapshot timeout")
        cumulative_loss = Decimal(self.observation_loss)
        fees = Decimal("0.02")
        net_pnl = -cumulative_loss
        gross_pnl = net_pnl + fees
        fetched_at = self.final_fetched_at
        evidence_label = "final"
        if is_post_halt:
            evidence_label = "post-halt-final"
            post_halt_fetched_at = self.post_halt_final_fetched_at
            if post_halt_fetched_at is not None:
                fetched_at = post_halt_fetched_at
        payload = self._identity()
        payload.update(
            {
                "target_symbol_flat": self.position_quantity == 0,
                "target_symbol_regular_orders_zero": True,
                "target_symbol_algo_orders_zero": True,
                "position_quantity": str(self.position_quantity),
                "non_target_portfolio_baseline_sha256": (
                    self.authorization.portfolio_baseline_sha256
                ),
                "gross_pnl_usdt": str(gross_pnl),
                "fees_usdt": str(fees),
                "net_pnl_usdt": str(net_pnl),
                "cumulative_net_loss_usdt": str(
                    cumulative_loss
                ),
                "source": self.final_source,
                "fetched_at": fetched_at.isoformat(),
                "evidence_sha256": _digest(evidence_label),
            }
        )
        return payload

    def halt(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self._require_operation_lock()
        self._record_call("halt", request)
        if self.calls.count("halt") <= self.halt_failures:
            raise RuntimeError("injected HALT failure")
        return self._ack(
            "halt",
            source=self.halt_source,
            trading_state=self.halt_state,
            observed_at=self.halt_observed_at.isoformat(),
            side_effect_id=request["side_effect_id"],
        )

    def _identity(self) -> dict[str, Any]:
        authorization = self.authorization
        return {
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
        }

    def _require_operation_lock(self) -> None:
        operation_lock = self.operation_lock
        if operation_lock is None:
            return
        operation_lock.require_held()

    def _record_call(
        self,
        action: str,
        request: Mapping[str, Any],
    ) -> None:
        self.calls.append(action)
        action_requests = self.requests.setdefault(action, [])
        action_requests.append(dict(request))

    def _ack(
        self,
        action: str,
        **extra: Any,
    ) -> Mapping[str, Any]:
        payload = self._identity()
        payload.update(
            {
                "accepted": True,
                "action": action,
                "evidence_sha256": _digest(action),
            }
        )
        payload.update(extra)
        return payload


def test_authorization_rejects_wrong_account(
    tmp_path: Path,
) -> None:
    paths = _write_authorization_files(
        tmp_path,
        mutate=lambda documents: documents["release_gate"].update(
            {"account_id": "account-b"}
        ),
    )

    with pytest.raises(
        executor.LiveTradeExecutionError,
        match="account must be account-a",
    ):
        executor.load_authorization(
            paths,
            execute_live=False,
            now=NOW,
        )


def test_authorization_rejects_wrong_symbol(
    tmp_path: Path,
) -> None:
    paths = _write_authorization_files(
        tmp_path,
        mutate=lambda documents: documents["release_gate"].update(
            {"symbol": "BTCUSDT"}
        ),
    )

    with pytest.raises(
        executor.LiveTradeExecutionError,
        match="symbol must be SOLUSDT",
    ):
        executor.load_authorization(
            paths,
            execute_live=False,
            now=NOW,
        )


def test_authorization_rejects_notional_over_12_usdt(
    tmp_path: Path,
) -> None:
    paths = _write_authorization_files(
        tmp_path,
        mutate=lambda documents: documents["permit"].update(
            {
                "max_notional_usdt": "12.1",
            }
        ),
        rebuild_hash_chain=True,
    )

    with pytest.raises(
        executor.LiveTradeExecutionError,
        match="max notional exceeds 12 USDT",
    ):
        executor.load_authorization(
            paths,
            execute_live=False,
            now=NOW,
        )


def test_authorization_requires_loss_threshold_below_1_5_usdt(
    tmp_path: Path,
) -> None:
    paths = _write_authorization_files(
        tmp_path,
        mutate=lambda documents: documents["permit"].update(
            {"max_cumulative_net_loss_usdt": "1.5"}
        ),
        rebuild_hash_chain=True,
    )

    with pytest.raises(
        executor.LiveTradeExecutionError,
        match="loss threshold must be below 1.5 USDT",
    ):
        executor.load_authorization(
            paths,
            execute_live=False,
            now=NOW,
        )


def test_live_authorization_verifies_four_signed_documents(
    tmp_path: Path,
) -> None:
    verifier = FakeSignatureVerifier()
    paths = _write_authorization_files(
        tmp_path,
        signed=True,
    )

    with _operation_lock(tmp_path) as operation_lock:
        authorization = executor.load_authorization(
            paths,
            execute_live=True,
            signature_verifier=verifier,
            operation_lock=operation_lock,
            now=NOW,
        )

    assert authorization.signatures_verified is True
    assert len(verifier.payload_hashes) == 4
    assert set(verifier.payload_hashes) == set(
        authorization.document_sha256.values()
    )


def test_live_authorization_rejects_missing_signature(
    tmp_path: Path,
) -> None:
    verifier = FakeSignatureVerifier()
    paths = _write_authorization_files(
        tmp_path,
        signed=True,
    )
    paths = executor.AuthorizationPaths(
        release_gate=paths.release_gate,
        safety_gate=executor.SignedDocumentPaths(
            paths.safety_gate.payload,
            None,
        ),
        emergency_close_gate=paths.emergency_close_gate,
        permit=paths.permit,
        reviewer_public_key=paths.reviewer_public_key,
    )

    with _operation_lock(tmp_path) as operation_lock:
        with pytest.raises(
            executor.LiveTradeExecutionError,
            match="safety_gate signature is required",
        ):
            executor.load_authorization(
                paths,
                execute_live=True,
                signature_verifier=verifier,
                operation_lock=operation_lock,
                now=NOW,
            )
    assert verifier.payload_hashes == []


def test_authorization_rejects_gate_hash_drift(
    tmp_path: Path,
) -> None:
    paths = _write_authorization_files(
        tmp_path,
        mutate=lambda documents: documents["safety_gate"].update(
            {"operator_note": "mutated after downstream hashes"}
        ),
    )

    with pytest.raises(
        executor.LiveTradeExecutionError,
        match="document hash mismatch: safety_gate_sha256",
    ):
        executor.load_authorization(
            paths,
            execute_live=False,
            now=NOW,
        )


def test_reviewer_public_key_fixture_matches_pinned_trust_root() -> None:
    actual_sha256 = hashlib.sha256(
        PINNED_REVIEWER_PUBLIC_KEY
    ).hexdigest()

    assert actual_sha256 == executor.PINNED_REVIEWER_PUBLIC_KEY_SHA256


def test_live_authorization_rejects_forged_self_signed_reviewer_key(
    tmp_path: Path,
) -> None:
    verifier = FakeSignatureVerifier()
    paths = _write_authorization_files(tmp_path, signed=True)
    assert paths.reviewer_public_key is not None
    paths.reviewer_public_key.chmod(0o600)
    paths.reviewer_public_key.write_bytes(b"forged-reviewer-public-key")
    paths.reviewer_public_key.chmod(0o400)

    with _operation_lock(tmp_path) as operation_lock:
        with pytest.raises(
            executor.LiveTradeExecutionError,
            match="reviewer public key hash mismatch",
        ):
            executor.load_authorization(
                paths,
                execute_live=True,
                signature_verifier=verifier,
                operation_lock=operation_lock,
                now=NOW,
            )

    assert verifier.payload_hashes == []


@pytest.mark.parametrize(
    "field_name",
    [
        "actor_tick_at",
        "loss_monitor_at",
    ],
)
def test_authorization_rejects_stale_hard_safety_timestamp(
    tmp_path: Path,
    field_name: str,
) -> None:
    stale_at = NOW - timedelta(seconds=6)
    paths = _write_authorization_files(
        tmp_path,
        mutate=lambda documents: documents["safety_gate"].update(
            {field_name: stale_at.isoformat()}
        ),
        rebuild_hash_chain=True,
    )

    with pytest.raises(
        executor.LiveTradeExecutionError,
        match=f"safety gate {field_name} is stale",
    ):
        executor.load_authorization(
            paths,
            execute_live=False,
            now=NOW,
        )


@pytest.mark.parametrize(
    "field_name",
    [
        "heartbeat_at",
        "projection_at",
        "reconciliation_at",
    ],
)
def test_authorization_accepts_stale_soft_safety_timestamp_with_warning(
    tmp_path: Path,
    field_name: str,
) -> None:
    stale_at = NOW - timedelta(seconds=6)
    paths = _write_authorization_files(
        tmp_path,
        mutate=lambda documents: documents["safety_gate"].update(
            {field_name: stale_at.isoformat()}
        ),
        rebuild_hash_chain=True,
    )

    authorization = executor.load_authorization(
        paths,
        execute_live=False,
        now=NOW,
    )

    assert any(
        f"safety gate {field_name} is stale" in warning
        for warning in authorization.warnings
    )


def test_authorization_soft_safety_truths_become_warnings(
    tmp_path: Path,
) -> None:
    paths = _write_authorization_files(
        tmp_path,
        mutate=lambda documents: documents["safety_gate"].update(
            {
                "readiness_healthy": False,
                "reconciliation_healthy": False,
                "no_open_p0_p1_incidents": False,
                "circuit_open": True,
                "memory_queue_pressure": True,
            }
        ),
        rebuild_hash_chain=True,
    )

    authorization = executor.load_authorization(
        paths,
        execute_live=False,
        now=NOW,
    )

    assert authorization.warnings == (
        "SAFETY_READINESS_DEGRADED: readiness_healthy=false",
        (
            "SAFETY_RECONCILIATION_DEGRADED: "
            "reconciliation_healthy=false"
        ),
        (
            "UNSCOPED_INCIDENT_WARNING: "
            "no_open_p0_p1_incidents=false"
        ),
        "CIRCUIT_OPEN_DEGRADED: circuit_open=true",
        (
            "MEMORY_QUEUE_PRESSURE_DEGRADED: "
            "memory_queue_pressure=true"
        ),
    )


def test_close_intent_and_client_order_id_are_distinct_and_stable(
    tmp_path: Path,
) -> None:
    paths = _write_authorization_files(tmp_path)

    first = executor.load_authorization(
        paths,
        execute_live=False,
        now=NOW,
    )
    restarted = executor.load_authorization(
        paths,
        execute_live=False,
        now=NOW,
    )

    assert restarted.close_intent_id == first.close_intent_id
    assert first.close_intent_id != first.intent_id
    assert UUID(first.close_intent_id).version == 5
    assert first.open_client_order_id == (
        f"B{UUID(first.intent_id).hex}01"
    )
    assert first.close_client_order_id == (
        f"B{UUID(first.close_intent_id).hex}01"
    )
    assert first.close_client_order_id != first.open_client_order_id


def test_live_operation_lock_covers_authorization_through_evidence_commit(
    tmp_path: Path,
) -> None:
    paths = _write_authorization_files(tmp_path, signed=True)
    adapter_path = _write_live_adapter(tmp_path)
    verifier = FakeSignatureVerifier()
    operation_lock = _operation_lock(tmp_path)
    evidence_path = tmp_path / "locked-lifecycle-evidence.json"
    store = _permit_store(tmp_path / "locked-lifecycle-ledger.json")

    with operation_lock:
        authorization = executor.load_authorization(
            paths,
            execute_live=True,
            signature_verifier=verifier,
            operation_lock=operation_lock,
            now=NOW,
        )
        validated_adapter = executor.validate_live_adapter(
            adapter_path,
            expected_sha256=(
                authorization.release.live_adapter_sha256
            ),
        )
        assert validated_adapter.source_path == adapter_path
        adapter = FakeAdapter(
            authorization,
            operation_lock=operation_lock,
        )
        live_executor = executor.AccountALiveTradeExecutor(
            adapter=adapter,
            permit_store=store,
            evidence_writer=LockCheckingEvidenceWriter(
                evidence_path,
                operation_lock,
            ),
            operation_lock=operation_lock,
            mode="live",
            max_observations=3,
            poll_interval_seconds=0,
            clock=lambda: NOW,
            sleeper=lambda _seconds: None,
        )

        result = live_executor.execute(authorization)
        assert result.passed is True
        assert operation_lock.is_held is True

    assert operation_lock.is_held is False
    with _operation_lock(tmp_path):
        pass


def test_live_operation_lock_conflict_precedes_verifier_and_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _authorization(tmp_path)
    verifier = FakeSignatureVerifier()
    adapter = FakeAdapter(authorization)
    first = _operation_lock(tmp_path)
    contender = _operation_lock(tmp_path)

    def reject_subprocess(*_args, **_kwargs):
        raise AssertionError("operation lock conflict started subprocess")

    monkeypatch.setattr(executor.subprocess, "run", reject_subprocess)

    with first:
        with pytest.raises(
            executor.OperationLockConflict,
            match="operation lock is busy",
        ):
            contender.acquire()

    assert verifier.payload_hashes == []
    assert adapter.calls == []


def test_live_adapter_accepts_only_signed_canonical_bytes(
    tmp_path: Path,
) -> None:
    adapter_path = _write_live_adapter(tmp_path)

    validated = executor.validate_live_adapter(
        adapter_path,
        expected_sha256=LIVE_ADAPTER_SHA256,
    )

    assert validated.source_path == adapter_path
    assert validated.sha256 == LIVE_ADAPTER_SHA256
    assert validated.payload == LIVE_ADAPTER_BYTES


def test_live_adapter_rejects_relative_path() -> None:
    with pytest.raises(
        executor.LiveTradeExecutionError,
        match="live adapter path must be absolute",
    ):
        executor.validate_live_adapter(
            Path("relative-live-adapter"),
            expected_sha256=LIVE_ADAPTER_SHA256,
        )


def test_live_adapter_rejects_symlink_path(
    tmp_path: Path,
) -> None:
    target = _write_live_adapter(tmp_path)
    symlink = tmp_path / "live-adapter-link"
    symlink.symlink_to(target)

    with pytest.raises(
        executor.LiveTradeExecutionError,
        match="canonical and contain no symlinks",
    ):
        executor.validate_live_adapter(
            symlink,
            expected_sha256=LIVE_ADAPTER_SHA256,
        )


def test_live_adapter_rejects_hash_drift(
    tmp_path: Path,
) -> None:
    adapter_path = _write_live_adapter(tmp_path)

    with pytest.raises(
        executor.LiveTradeExecutionError,
        match="hash differs from signed release",
    ):
        executor.validate_live_adapter(
            adapter_path,
            expected_sha256="f" * 64,
        )


def test_live_adapter_rejects_group_writable_file(
    tmp_path: Path,
) -> None:
    adapter_path = _write_live_adapter(tmp_path)
    adapter_path.chmod(0o520)

    with pytest.raises(
        executor.LiveTradeExecutionError,
        match="group/world writable",
    ):
        executor.validate_live_adapter(
            adapter_path,
            expected_sha256=LIVE_ADAPTER_SHA256,
        )


def test_json_adapter_caps_action_timeout_to_recovery_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_path = _write_live_adapter(tmp_path)
    validated = executor.validate_live_adapter(
        adapter_path,
        expected_sha256=LIVE_ADAPTER_SHA256,
    )
    observed_timeouts: list[float] = []

    def timeout_run(*_args, **kwargs):
        observed_timeouts.append(float(kwargs["timeout"]))
        raise subprocess.TimeoutExpired(
            cmd="reviewed-live-adapter close",
            timeout=kwargs["timeout"],
        )

    monkeypatch.setattr(executor.subprocess, "run", timeout_run)

    with executor.JsonCommandAdapter(
        validated,
        timeout_seconds=15,
        live_authorized=True,
        operation_lock=AlwaysHeldOperationLock(),
    ) as adapter, pytest.raises(
        executor.LiveTradeExecutionError,
        match="adapter action timed out: close",
    ):
        adapter.submit_close(
            {
                "hard_timeout_seconds": 2.5,
            }
        )

    assert observed_timeouts == [2.5]


def test_live_store_rejects_nonfixed_path_without_testing_override(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    store = executor.SingleUsePermitStore(
        tmp_path / "replaceable-live-ledger.json",
        clock=lambda: NOW,
    )

    with pytest.raises(
        executor.LiveTradeExecutionError,
        match="live permit ledger path is fixed",
    ):
        store.validate_live_binding(authorization)


def test_live_store_rejects_signed_store_identity_mismatch(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    store = executor.SingleUsePermitStore(
        tmp_path / "attacker-ledger.json",
        store_id="attacker-controlled-store",
        testing_allow_live_path_override=True,
        clock=lambda: NOW,
    )

    with pytest.raises(
        executor.LiveTradeExecutionError,
        match="permit store identity differs from signed release",
    ):
        store.validate_live_binding(authorization)


def test_live_store_rejects_ledger_copied_to_another_path(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    source_path = tmp_path / "source-live-ledger.json"
    source_store = _permit_store(source_path)
    source_store.claim(authorization, mode="live")
    replay_path = tmp_path / "replayed-live-ledger.json"
    replay_path.write_bytes(source_path.read_bytes())
    replay_path.chmod(0o600)
    replay_store = _permit_store(replay_path)

    with pytest.raises(
        executor.LiveTradeExecutionError,
        match="canonical path mismatch",
    ):
        replay_store.claim_or_recover(
            authorization,
            mode="live",
        )


def test_single_use_store_rejects_repeated_permit(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    store = _permit_store(tmp_path / "permit-ledger.json")

    store.claim(authorization, mode="live")

    with pytest.raises(
        executor.DuplicatePermitError,
        match="already has durable state",
    ):
        store.claim(authorization, mode="live")


def test_single_use_store_rejects_symlink_lock(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    ledger_path = tmp_path / "symlink-ledger.json"
    lock_path = tmp_path / "symlink-ledger.json.lock"
    lock_target = tmp_path / "lock-target"
    lock_target.write_text("", encoding="ascii")
    lock_path.symlink_to(lock_target)
    store = _permit_store(ledger_path)

    with pytest.raises(
        executor.LiveTradeExecutionError,
        match="cannot open protected lock file",
    ):
        store.claim(authorization, mode="live")


def test_duplicate_permit_execution_halts_and_writes_failure_evidence(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    store = _permit_store(tmp_path / "duplicate-ledger.json")
    store.claim(authorization, mode="live")
    adapter = FakeAdapter(authorization)
    evidence_path = tmp_path / "duplicate-evidence.json"
    live_executor = executor.AccountALiveTradeExecutor(
        adapter=adapter,
        permit_store=store,
        evidence_writer=executor.AtomicEvidenceWriter(evidence_path),
        operation_lock=AlwaysHeldOperationLock(),
        mode="live",
        max_observations=3,
        poll_interval_seconds=0,
        clock=lambda: NOW,
        sleeper=lambda _seconds: None,
    )

    result = live_executor.execute(authorization)

    assert result.passed is False
    assert "incomplete permit journal requires recovery" in (
        result.failure_reason
    )
    assert adapter.calls == [
        "cancel-open",
        "position",
        "final-snapshot",
        "halt",
        "final-snapshot",
    ]
    evidence = json.loads(evidence_path.read_text(encoding="ascii"))
    assert evidence["passed"] is False
    assert evidence["finished_halted"] is True


def test_open_exception_still_closes_exact_position_then_halts(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        fail_observe=True,
    )
    evidence_path = tmp_path / "failure-evidence.json"
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=evidence_path,
    )

    result = live_executor.execute(authorization)

    assert result.passed is False
    assert "injected observation failure" in result.failure_reason
    assert result.close_submitted is True
    assert result.close_quantity == Decimal("0.1")
    assert adapter.calls.index("close") < adapter.calls.index("halt")
    assert adapter.close_requests == [
        {
            **_base_request(authorization),
            "mode": "live",
            "authorization_sha256": (
                authorization.authorization_sha256
            ),
            "document_sha256": dict(
                authorization.document_sha256
            ),
            "client_order_id": (
                authorization.close_client_order_id
            ),
            "intent_id": authorization.close_intent_id,
            "open_intent_id": authorization.intent_id,
            "side": "SELL",
            "position_side": "LONG",
            "order_type": "MARKET",
            "quantity": "0.1",
            "reduce_only": True,
            "reason": "failure-cleanup",
            "attempt": 1,
            "side_effect_id": executor.deterministic_side_effect_id(
                authorization,
                "CLOSE",
            ),
            "hard_timeout_seconds": 20.0,
        }
    ]
    evidence = json.loads(evidence_path.read_text(encoding="ascii"))
    assert evidence["passed"] is False
    assert evidence["finished_halted"] is True
    assert evidence["mainnet_round_trip"]["close_reduce_only"] is True
    assert evidence["mainnet_round_trip"]["close_quantity"] == "0.1"
    assert evidence["testnet_emergency_close"]["verified"] is True


@pytest.mark.parametrize(
    ("action", "phase", "expected_close_count"),
    [
        ("RESUME", "complete", 0),
        ("OPEN", "complete", 1),
        ("CLOSE", "prepare", 1),
        ("CLOSE", "complete", 1),
        ("HALT", "prepare", 1),
        ("HALT", "complete", 1),
    ],
)
def test_journal_fsync_failure_cannot_block_emergency_close_or_halt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    phase: str,
    expected_close_count: int,
) -> None:
    authorization = _authorization(tmp_path)
    store = _permit_store(
        tmp_path / f"{action.lower()}-{phase}-ledger.json"
    )
    failure_count = _inject_journal_fsync_failure(
        monkeypatch,
        action=action,
        phase=phase,
    )
    adapter = FakeAdapter(authorization)
    evidence_path = tmp_path / f"{action.lower()}-{phase}.json"
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=evidence_path,
        permit_store=store,
    )

    result = live_executor.execute(authorization)

    assert failure_count == [f"{phase}:{action}"]
    assert result.passed is False
    assert result.finished_halted is True
    assert "permit journal" in result.failure_reason
    assert "No space left on device" in result.failure_reason
    assert adapter.calls.count("open") <= 1
    assert adapter.calls.count("close") == expected_close_count
    assert adapter.calls.count("halt") == 1
    assert adapter.calls.index("position") < adapter.calls.index("halt")
    if expected_close_count == 1:
        assert adapter.calls.index("position") < adapter.calls.index(
            "close"
        )
        assert adapter.calls.index("close") < adapter.calls.index(
            "halt"
        )
    evidence = json.loads(evidence_path.read_text(encoding="ascii"))
    assert evidence["passed"] is False
    assert evidence["finished_halted"] is True
    assert evidence["permit_journal_degraded"] is True
    assert len(evidence["permit_journal_failures"]) == 1
    outcome = store.claim_or_recover(
        authorization,
        mode="live",
    )
    assert outcome.recovery_required is True
    assert outcome.terminal is False
    with pytest.raises(
        executor.DuplicatePermitError,
        match="already has durable state",
    ):
        store.claim(authorization, mode="live")


def test_stalled_journal_write_times_out_before_emergency_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _authorization(tmp_path)
    store = _permit_store(tmp_path / "stalled-journal-ledger.json")
    adapter = FakeAdapter(authorization)
    release_write = threading.Event()
    write_started = threading.Event()
    write_finished = threading.Event()
    original_atomic_write_json = executor._atomic_write_json

    def blocking_atomic_write_json(
        path: Path,
        payload: Mapping[str, Any],
        *,
        mode: int,
    ) -> None:
        records = payload.get("records")
        should_block = False
        if isinstance(records, dict):
            for record in records.values():
                if not isinstance(record, dict):
                    continue
                if record.get("state") == "RESUMED":
                    should_block = True
                    break
        if not should_block:
            original_atomic_write_json(path, payload, mode=mode)
            return
        write_started.set()
        try:
            release_write.wait(timeout=5)
            original_atomic_write_json(path, payload, mode=mode)
        finally:
            write_finished.set()

    monkeypatch.setattr(
        executor,
        "_atomic_write_json",
        blocking_atomic_write_json,
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "stalled-journal.json",
        permit_store=store,
        journal_write_timeout_seconds=0.01,
    )

    try:
        result = live_executor.execute(authorization)
        assert write_started.is_set() is True
        assert release_write.is_set() is False
    finally:
        release_write.set()

    assert write_finished.wait(timeout=1) is True
    assert result.passed is False
    assert "journal write exceeded live safety timeout" in (
        result.failure_reason
    )
    assert adapter.calls == [
        "resume",
        "cancel-open",
        "position",
        "final-snapshot",
        "halt",
        "final-snapshot",
    ]
    assert result.finished_halted is True
    evidence = json.loads(
        (tmp_path / "stalled-journal.json").read_text(
            encoding="ascii"
        )
    )
    assert evidence["permit_journal_degraded"] is True


def test_open_timeout_after_exchange_acceptance_recovers_exchange_first(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        open_error_after_effect=TimeoutError(
            "injected open response timeout"
        ),
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "open-timeout.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert result.passed is False
    assert result.error_code == "OPEN_RESULT_AMBIGUOUS"
    assert result.retryable is False
    assert "injected open response timeout" in result.failure_reason
    assert adapter.calls.count("open") == 1
    assert adapter.calls == [
        "resume",
        "open",
        "cancel-open",
        "position",
        "close",
        "position",
        "final-snapshot",
        "halt",
        "final-snapshot",
    ]
    assert result.close_submitted is True
    assert result.finished_halted is True


def test_resume_timeout_recovers_as_degraded_with_bounded_retry(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        resume_failures=1,
    )
    evidence_path = tmp_path / "resume-recovered.json"
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=evidence_path,
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert result.passed is True
    assert result.retryable is False
    assert result.retry_counts["RESUME"] == 1
    assert adapter.calls.count("resume") == 2
    assert adapter.calls.count("open") == 1
    evidence = json.loads(evidence_path.read_text(encoding="ascii"))
    assert evidence["result_status"] == "DEGRADED"
    assert evidence["retry_counts"]["RESUME"] == 1
    assert any(
        "RESUME recovered after 1 retry" in reason
        for reason in evidence["degraded_reasons"]
    )


def test_resume_timeout_exhaustion_continues_open_as_degraded(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    store = _permit_store(tmp_path / "resume-degraded-ledger.json")
    adapter = FakeAdapter(
        authorization,
        resume_failures=3,
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "resume-degraded.json",
        permit_store=store,
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert result.passed is True
    assert result.retryable is False
    assert result.error_code == "SOFT_TRANSPORT_DEGRADED"
    assert adapter.calls.count("resume") == 3
    assert adapter.calls.count("open") == 1
    snapshot = store.snapshot(authorization, mode="live")
    assert snapshot is not None
    assert snapshot["state"] == "EVIDENCE_COMMITTED"


def test_observe_timeout_recovers_as_degraded_without_open_replay(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        observe_failures=1,
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "observe-recovered.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert result.passed is True
    assert result.retry_counts["OBSERVE"] == 1
    assert adapter.calls.count("open") == 1
    assert adapter.calls.count("observe") == 2


def test_observe_timeout_exhaustion_closes_exactly_as_degraded(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        observe_failures=3,
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "observe-exhausted.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert result.passed is True
    assert result.retryable is False
    assert result.error_code == "SOFT_TRANSPORT_DEGRADED"
    assert adapter.calls.count("open") == 1
    assert adapter.calls.count("observe") == 3
    assert result.close_submitted is True


def test_close_timeout_after_exchange_acceptance_queries_before_retry(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        close_error_after_effect=TimeoutError(
            "injected close response timeout"
        ),
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "close-timeout.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert result.passed is True
    assert any(
        "CLOSE transport failure" in reason
        for reason in result.degraded_reasons
    )
    assert adapter.calls.count("close") == 1
    close_index = adapter.calls.index("close")
    assert adapter.calls[close_index + 1] == "position"
    assert result.close_submitted is True
    assert result.finished_halted is True


def test_final_pass_uses_fresh_post_halt_exchange_snapshot(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)
    evidence_path = tmp_path / "post-halt-final.json"
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=evidence_path,
    )

    result = live_executor.execute(authorization)

    assert result.status == "PASSED"
    assert result.passed is True
    final_indexes = [
        index
        for index, action in enumerate(adapter.calls)
        if action == "final-snapshot"
    ]
    halt_index = adapter.calls.index("halt")
    assert len(final_indexes) == 2
    assert final_indexes[0] < halt_index < final_indexes[1]
    final_requests = adapter.requests["final-snapshot"]
    assert final_requests[0]["phase"] == "pre-halt-close-proof"
    assert final_requests[1]["phase"] == "post-halt-final"
    evidence = json.loads(evidence_path.read_text(encoding="ascii"))
    round_trip = evidence["mainnet_round_trip"]
    assert round_trip["final_snapshot_phase"] == "post-halt-final"
    assert round_trip["final_snapshot_fetched_at"] == NOW.isoformat()
    assert round_trip["final_snapshot_evidence_sha256"] == _digest(
        "post-halt-final"
    )


def test_stale_post_halt_snapshot_blocks_final_pass(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        post_halt_final_fetched_at=NOW - timedelta(seconds=6),
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "stale-post-halt-final.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert result.passed is False
    assert result.finished_halted is True
    assert result.error_code == "POST_HALT_SNAPSHOT_UNPROVEN"
    assert "final snapshot fetched_at is stale" in result.failure_reason
    assert adapter.calls[-1] == "final-snapshot"


def test_close_request_uses_independent_derived_intent(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "close-intent.json",
    )

    result = live_executor.execute(authorization)

    assert result.passed is True
    open_request = adapter.requests["open"][0]
    close_request = adapter.requests["close"][0]
    assert open_request["intent_id"] == authorization.intent_id
    assert close_request["intent_id"] == authorization.close_intent_id
    assert close_request["intent_id"] != open_request["intent_id"]
    assert close_request["client_order_id"] == (
        f"B{UUID(close_request['intent_id']).hex}01"
    )


def test_live_side_effect_identity_is_stable_across_retries(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        close_failures=1,
        halt_failures=2,
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "stable-side-effects.json",
    )

    result = live_executor.execute(authorization)

    assert result.passed is True
    _assert_single_side_effect_identity(authorization, adapter)


def test_close_and_halt_receive_independent_hard_deadlines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _authorization(tmp_path)
    monotonic = MutableMonotonic()
    adapter = FakeAdapter(authorization)
    original_cancel_open = adapter.cancel_open
    original_submit_close = adapter.submit_close

    def slow_cancel_open(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        result = original_cancel_open(request)
        monotonic.advance(19)
        return result

    def slow_submit_close(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        result = original_submit_close(request)
        monotonic.advance(19)
        return result

    monkeypatch.setattr(adapter, "cancel_open", slow_cancel_open)
    monkeypatch.setattr(adapter, "submit_close", slow_submit_close)
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "independent-deadlines.json",
        monotonic=monotonic,
        recovery_deadline_seconds=20,
    )

    result = live_executor.execute(authorization)

    assert result.passed is True
    close_request = adapter.requests["close"][0]
    halt_request = adapter.requests["halt"][0]
    assert close_request["hard_timeout_seconds"] == pytest.approx(20)
    assert halt_request["hard_timeout_seconds"] == pytest.approx(20)


def test_cleanup_orchestration_exception_still_halts_and_writes_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        fail_observe=True,
    )
    evidence_path = tmp_path / "cleanup-failure-evidence.json"
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=evidence_path,
    )

    def fail_cleanup(*_args, **_kwargs):
        raise RuntimeError("injected cleanup orchestration failure")

    monkeypatch.setattr(
        live_executor,
        "_flatten_target_risk",
        fail_cleanup,
    )

    result = live_executor.execute(authorization)

    assert result.passed is False
    assert "cleanup orchestration failed" in result.failure_reason
    assert adapter.calls[-1] == "halt"
    evidence = json.loads(evidence_path.read_text(encoding="ascii"))
    assert evidence["passed"] is False
    assert evidence["finished_halted"] is True


def test_adapter_identity_mismatch_closes_and_halts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)
    original_observe = adapter.observe

    def mismatched_observe(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        payload = dict(original_observe(request))
        payload["release_id"] = "other-release"
        return payload

    monkeypatch.setattr(adapter, "observe", mismatched_observe)
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "identity-evidence.json",
    )

    result = live_executor.execute(authorization)

    assert result.passed is False
    assert "adapter identity mismatch: release_id" in (
        result.failure_reason
    )
    assert result.close_submitted is True
    assert adapter.calls[-2:] == ["halt", "final-snapshot"]


def test_adapter_side_effect_identity_mismatch_recovers_without_open_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)
    original_submit_open = adapter.submit_open

    def mismatched_submit_open(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        payload = dict(original_submit_open(request))
        payload["side_effect_id"] = "f" * 64
        return payload

    monkeypatch.setattr(
        adapter,
        "submit_open",
        mismatched_submit_open,
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "side-effect-mismatch.json",
    )

    result = live_executor.execute(authorization)

    assert result.passed is False
    assert "adapter side effect identity mismatch: open" in (
        result.failure_reason
    )
    assert adapter.calls.count("open") == 1
    assert result.close_submitted is True
    assert result.finished_halted is True


def test_actual_open_notional_breach_closes_and_halts(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        observation_price="121",
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "notional-evidence.json",
    )

    result = live_executor.execute(authorization)

    assert result.passed is False
    assert "actual open notional exceeds permit" in result.failure_reason
    assert result.close_submitted is True
    assert adapter.calls[-2:] == ["halt", "final-snapshot"]
    evidence = json.loads(
        (tmp_path / "notional-evidence.json").read_text(
            encoding="ascii"
        )
    )
    assert (
        evidence["mainnet_round_trip"]["actual_open_notional_usdt"]
        == "12.1"
    )


def test_loss_threshold_reached_closes_and_halts_immediately(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        observation_loss="1.49",
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "loss-evidence.json",
    )

    result = live_executor.execute(authorization)

    assert result.passed is False
    assert (
        "cumulative net loss threshold reached"
        in result.failure_reason
    )
    assert result.close_submitted is True
    assert adapter.calls == [
        "resume",
        "open",
        "observe",
        "cancel-open",
        "position",
        "close",
        "position",
        "final-snapshot",
        "halt",
        "final-snapshot",
    ]
    evidence = json.loads(
        (tmp_path / "loss-evidence.json").read_text(
            encoding="ascii"
        )
    )
    assert (
        evidence["mainnet_round_trip"][
            "cumulative_net_loss_usdt"
        ]
        == "1.49"
    )


@pytest.mark.parametrize(
    "field_name",
    [
        "actor_tick_at",
        "loss_monitor_at",
    ],
)
def test_hard_health_is_revalidated_after_resume_before_open(
    tmp_path: Path,
    field_name: str,
) -> None:
    fresh_at_open = NOW + timedelta(seconds=6)
    health_times = {
        "actor_tick_at": fresh_at_open,
        "heartbeat_at": fresh_at_open,
        "projection_at": fresh_at_open,
        "reconciliation_at": fresh_at_open,
        "loss_monitor_at": fresh_at_open,
    }
    health_times[field_name] = NOW
    authorization = replace(
        _authorization(tmp_path),
        **health_times,
    )
    clock = MutableClock()

    def advance_clock_after_resume() -> None:
        clock.current = fresh_at_open

    adapter = FakeAdapter(
        authorization,
        on_resume=advance_clock_after_resume,
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / f"stale-{field_name}.json",
        clock=clock,
    )

    result = live_executor.execute(authorization)

    assert result.passed is False
    assert f"before OPEN {field_name} is stale" in (
        result.failure_reason
    )
    assert "open" not in adapter.calls
    assert adapter.calls[-1] == "halt"


@pytest.mark.parametrize(
    "field_name",
    [
        "heartbeat_at",
        "projection_at",
        "reconciliation_at",
    ],
)
def test_soft_health_staleness_after_resume_continues_open(
    tmp_path: Path,
    field_name: str,
) -> None:
    fresh_at_open = NOW + timedelta(seconds=6)
    health_times = {
        "actor_tick_at": fresh_at_open,
        "heartbeat_at": fresh_at_open,
        "projection_at": fresh_at_open,
        "reconciliation_at": fresh_at_open,
        "loss_monitor_at": fresh_at_open,
    }
    health_times[field_name] = NOW
    authorization = replace(
        _authorization(tmp_path),
        **health_times,
    )
    clock = MutableClock()

    def advance_clock_after_resume() -> None:
        clock.current = fresh_at_open

    adapter = FakeAdapter(
        authorization,
        on_resume=advance_clock_after_resume,
        observation_times={
            "observed_at": fresh_at_open,
            "mark_at": fresh_at_open,
            "loss_monitor_at": fresh_at_open,
        },
        position_fetched_at=fresh_at_open,
        final_fetched_at=fresh_at_open,
        post_halt_final_fetched_at=fresh_at_open,
        halt_observed_at=fresh_at_open,
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / f"soft-stale-{field_name}.json",
        clock=clock,
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert result.passed is True
    assert adapter.calls.count("open") == 1
    assert any(
        f"before OPEN {field_name} is stale" in reason
        for reason in result.degraded_reasons
    )


@pytest.mark.parametrize(
    "field_name",
    [
        "observed_at",
        "mark_at",
        "loss_monitor_at",
    ],
)
def test_stale_trade_observation_closes_and_halts(
    tmp_path: Path,
    field_name: str,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        observation_times={
            field_name: NOW - timedelta(seconds=6),
        },
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / f"stale-observation-{field_name}.json",
    )

    result = live_executor.execute(authorization)

    assert result.passed is False
    assert f"observation {field_name} is stale" in result.failure_reason
    assert result.close_submitted is True
    assert adapter.calls.index("close") < adapter.calls.index("halt")


@pytest.mark.parametrize(
    ("adapter_kwargs", "expected_error"),
    [
        (
            {"position_source": "projection"},
            "position source must be exchange",
        ),
        (
            {
                "position_fetched_at": NOW - timedelta(seconds=6),
            },
            "position fetched_at is stale",
        ),
        (
            {"final_source": "projection"},
            "final snapshot source must be exchange",
        ),
        (
            {
                "final_fetched_at": NOW - timedelta(seconds=6),
            },
            "final snapshot fetched_at is stale",
        ),
    ],
)
def test_cleanup_requires_fresh_exchange_authority(
    tmp_path: Path,
    adapter_kwargs: Mapping[str, Any],
    expected_error: str,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        fail_observe=True,
        **adapter_kwargs,
    )
    store = _permit_store(tmp_path / "exchange-authority-ledger.json")
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "exchange-authority.json",
        permit_store=store,
    )

    result = live_executor.execute(authorization)

    assert result.passed is False
    assert expected_error in result.failure_reason
    outcome = store.claim_or_recover(
        authorization,
        mode="live",
    )
    assert outcome.recovery_required is True
    assert outcome.terminal is False


@pytest.mark.parametrize(
    ("adapter_kwargs", "expected_error"),
    [
        (
            {"halt_source": "control-plane"},
            "HALT acknowledgement source must be node",
        ),
        (
            {"halt_state": "ACTIVE"},
            "did not prove HALTED or STOPPED",
        ),
        (
            {
                "halt_observed_at": NOW - timedelta(seconds=6),
            },
            "HALT acknowledgement observed_at is stale",
        ),
    ],
)
def test_halt_requires_fresh_node_state_proof(
    tmp_path: Path,
    adapter_kwargs: Mapping[str, Any],
    expected_error: str,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        **adapter_kwargs,
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "halt-state-proof.json",
    )

    result = live_executor.execute(authorization)

    assert result.passed is False
    assert result.finished_halted is False
    assert adapter.calls.count("halt") == 3
    assert expected_error in result.failure_reason


@pytest.mark.parametrize(
    "signal_number",
    [signal.SIGINT, signal.SIGTERM],
)
def test_termination_signal_runs_recovery_cleanup_and_halt(
    tmp_path: Path,
    signal_number: int,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        signal_number=signal_number,
    )
    store = _permit_store(
        tmp_path / f"signal-{signal_number}-ledger.json"
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / f"signal-{signal_number}.json",
        permit_store=store,
    )

    with executor.TerminationSignalGuard():
        result = live_executor.execute(authorization)

    assert result.passed is False
    assert "termination signal received" in result.failure_reason
    assert result.close_submitted is True
    assert adapter.calls.index("close") < adapter.calls.index("halt")
    _assert_single_side_effect_identity(authorization, adapter)
    snapshot = store.snapshot(authorization, mode="live")
    assert snapshot is not None
    assert snapshot["state"] == "EVIDENCE_COMMITTED"


def test_incomplete_open_journal_recovers_without_resume_or_open_replay(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    store = _permit_store(tmp_path / "incomplete-open-ledger.json")
    _seed_open_submitted_journal(store, authorization)
    adapter = FakeAdapter(authorization)
    adapter.position_quantity = authorization.quantity
    adapter.position_side = "LONG"
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "incomplete-open-recovery.json",
        permit_store=store,
    )

    result = live_executor.execute(authorization)

    assert result.passed is False
    assert "incomplete permit journal requires recovery" in (
        result.failure_reason
    )
    assert "resume" not in adapter.calls
    assert "open" not in adapter.calls
    assert adapter.calls == [
        "cancel-open",
        "position",
        "close",
        "position",
        "final-snapshot",
        "halt",
        "final-snapshot",
    ]
    snapshot = store.snapshot(authorization, mode="live")
    assert snapshot is not None
    assert snapshot["state"] == "EVIDENCE_COMMITTED"


def test_close_retry_exhaustion_keeps_journal_recoverable(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        fail_observe=True,
        close_failures=3,
    )
    store = _permit_store(tmp_path / "close-exhausted-ledger.json")
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "close-exhausted.json",
        permit_store=store,
        recovery_max_attempts=3,
    )

    result = live_executor.execute(authorization)

    assert result.passed is False
    assert adapter.calls.count("close") == 3
    assert [
        request["attempt"] for request in adapter.close_requests
    ] == [1, 2, 3]
    assert result.finished_halted is True
    outcome = store.claim_or_recover(
        authorization,
        mode="live",
    )
    assert outcome.recovery_required is True
    assert outcome.terminal is False
    assert outcome.state != "EVIDENCE_COMMITTED"


def test_halt_retry_exhaustion_keeps_journal_recoverable(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        halt_failures=3,
    )
    store = _permit_store(tmp_path / "halt-exhausted-ledger.json")
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "halt-exhausted.json",
        permit_store=store,
        recovery_max_attempts=3,
    )

    result = live_executor.execute(authorization)

    assert result.passed is False
    assert result.finished_halted is False
    assert adapter.calls.count("halt") == 3
    assert "unable to prove HALTED within recovery budget" in (
        result.failure_reason
    )
    outcome = store.claim_or_recover(
        authorization,
        mode="live",
    )
    assert outcome.recovery_required is True
    assert outcome.terminal is False


def test_successful_round_trip_binds_ids_and_evidence_chain(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)
    evidence_path = tmp_path / "success-evidence.json"
    store = _permit_store(tmp_path / "success-ledger.json")
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=evidence_path,
        permit_store=store,
    )

    result = live_executor.execute(authorization)

    assert result.passed is True
    evidence = json.loads(evidence_path.read_text(encoding="ascii"))
    assert evidence["rollout_phase"] == "account_a_canary"
    assert evidence["round_trip_count"] == 1
    assert evidence["permit_id"] == authorization.permit_id
    assert evidence["intent_id"] == authorization.intent_id
    assert (
        evidence["open_client_order_id"]
        == authorization.open_client_order_id
    )
    assert (
        evidence["close_client_order_id"]
        == authorization.close_client_order_id
    )
    assert evidence["side_effect_ids"] == {
        "resume": executor.deterministic_side_effect_id(
            authorization,
            "RESUME",
        ),
        "open": executor.deterministic_side_effect_id(
            authorization,
            "OPEN",
        ),
        "cancel_open": executor.deterministic_side_effect_id(
            authorization,
            "CANCEL_OPEN",
        ),
        "close": executor.deterministic_side_effect_id(
            authorization,
            "CLOSE",
        ),
        "halt": executor.deterministic_side_effect_id(
            authorization,
            "HALT",
        ),
    }
    round_trip = evidence["mainnet_round_trip"]
    assert round_trip["gross_pnl_usdt"] == "-0.08"
    assert round_trip["fees_usdt"] == "0.02"
    assert round_trip["net_pnl_usdt"] == "-0.10"
    assert round_trip["cumulative_net_loss_usdt"] == "0.10"
    assert round_trip["emergency_close_available"] is True
    previous_hash = "0" * 64
    for event in evidence["events"]:
        event_hash = event.pop("event_sha256")
        assert event["previous_event_sha256"] == previous_hash
        assert _digest_json(event) == event_hash
        previous_hash = event_hash
    assert evidence["event_chain_sha256"] == previous_hash
    snapshot = store.snapshot(authorization, mode="live")
    assert snapshot is not None
    assert snapshot["state"] == "EVIDENCE_COMMITTED"
    history_states = [
        entry["state"] for entry in snapshot["history"]
    ]
    required_states = [
        "AUTHORIZED",
        "RESUMED",
        "OPEN_SUBMITTED",
        "OPEN_OBSERVED",
        "CLOSE_PENDING",
        "CLOSE_CONFIRMED",
        "HALTED",
        "EVIDENCE_COMMITTED",
    ]
    state_indexes = [
        history_states.index(state) for state in required_states
    ]
    assert state_indexes == sorted(state_indexes)


def test_restart_commits_evidence_prepared_before_publication_crash(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    store_path = tmp_path / "prepare-crash-ledger.json"
    store = _permit_store(store_path)
    evidence_path = tmp_path / "prepare-crash-evidence.json"
    crashing_writer = CrashBeforePublishEvidenceWriter(evidence_path)
    first_adapter = FakeAdapter(authorization)
    first_executor = _executor(
        tmp_path,
        authorization,
        first_adapter,
        evidence_path=evidence_path,
        permit_store=store,
        evidence_writer=crashing_writer,
    )

    with pytest.raises(
        InjectedCrash,
        match="before evidence publication",
    ):
        first_executor.execute(authorization)

    assert evidence_path.exists() is False
    prepared_snapshot = store.snapshot(
        authorization,
        mode="live",
    )
    assert prepared_snapshot is not None
    assert prepared_snapshot["state"] == "HALTED"
    assert prepared_snapshot["pending_action"] == "PUBLISH_EVIDENCE"
    pending = prepared_snapshot["pending_evidence"]
    assert pending["path"] == str(evidence_path)

    restart_store = _permit_store(store_path)
    restart_adapter = FakeAdapter(authorization)
    restart_executor = _executor(
        tmp_path,
        authorization,
        restart_adapter,
        evidence_path=evidence_path,
        permit_store=restart_store,
    )

    result = restart_executor.execute(authorization)

    assert result.passed is True
    assert "resume" not in restart_adapter.calls
    assert "open" not in restart_adapter.calls
    _assert_single_side_effect_identity(
        authorization,
        first_adapter,
        restart_adapter,
    )
    assert evidence_path.exists() is True
    finalized = restart_store.snapshot(
        authorization,
        mode="live",
    )
    assert finalized is not None
    assert finalized["state"] == "EVIDENCE_COMMITTED"
    assert finalized["evidence_sha256"] == result.evidence_sha256


def test_restart_finalizes_ledger_after_evidence_publication_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _authorization(tmp_path)
    store_path = tmp_path / "commit-crash-ledger.json"
    store = _permit_store(store_path)
    evidence_path = tmp_path / "commit-crash-evidence.json"
    adapter = FakeAdapter(authorization)
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=evidence_path,
        permit_store=store,
    )

    def crash_before_ledger_finalize(
        _authorization: executor.CanaryAuthorization,
        *,
        mode: str,
        evidence_sha256: str,
    ) -> None:
        del mode
        del evidence_sha256
        raise InjectedCrash("crash before ledger finalize")

    monkeypatch.setattr(
        store,
        "commit_evidence",
        crash_before_ledger_finalize,
    )

    with pytest.raises(
        InjectedCrash,
        match="before ledger finalize",
    ):
        live_executor.execute(authorization)

    assert evidence_path.exists() is True
    restart_store = _permit_store(store_path)
    prepared_snapshot = restart_store.snapshot(
        authorization,
        mode="live",
    )
    assert prepared_snapshot is not None
    assert prepared_snapshot["state"] == "HALTED"
    pending_hash = prepared_snapshot["pending_evidence"]["sha256"]
    assert hashlib.sha256(evidence_path.read_bytes()).hexdigest() == (
        pending_hash
    )

    restart_adapter = FakeAdapter(authorization)
    restart_executor = _executor(
        tmp_path,
        authorization,
        restart_adapter,
        evidence_path=evidence_path,
        permit_store=restart_store,
    )

    result = restart_executor.execute(authorization)

    assert result.evidence_sha256 == pending_hash
    assert "resume" not in restart_adapter.calls
    assert "open" not in restart_adapter.calls
    finalized = restart_store.snapshot(
        authorization,
        mode="live",
    )
    assert finalized is not None
    assert finalized["state"] == "EVIDENCE_COMMITTED"


def test_atomic_evidence_write_fsyncs_file_and_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "evidence.json"
    writer = executor.AtomicEvidenceWriter(target)
    fsync_calls: list[int] = []
    link_calls: list[tuple[str, str]] = []
    original_fsync = executor.os.fsync
    original_link = executor.os.link

    def tracking_fsync(descriptor: int) -> None:
        fsync_calls.append(descriptor)
        original_fsync(descriptor)

    def tracking_link(source: str, destination: Path) -> None:
        link_calls.append((str(source), str(destination)))
        original_link(source, destination)

    monkeypatch.setattr(executor.os, "fsync", tracking_fsync)
    monkeypatch.setattr(executor.os, "link", tracking_link)

    evidence_sha256 = writer.write(
        {
            "schema_version": executor.EVIDENCE_SCHEMA,
            "passed": False,
        }
    )

    assert len(fsync_calls) >= 2
    assert len(link_calls) == 1
    assert link_calls[0][1] == str(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o400
    assert evidence_sha256 == hashlib.sha256(
        target.read_bytes()
    ).hexdigest()
    assert not list(tmp_path.glob(".evidence.json.*.tmp"))


def test_external_adapter_requires_verified_live_authorization() -> None:
    validated_adapter = executor.ValidatedLiveAdapter(
        source_path=Path("/tmp/reviewed-adapter"),
        sha256=LIVE_ADAPTER_SHA256,
        payload=LIVE_ADAPTER_BYTES,
    )
    with pytest.raises(
        executor.LiveTradeExecutionError,
        match="requires verified live authorization",
    ):
        executor.JsonCommandAdapter(
            validated_adapter,
            live_authorized=False,
            operation_lock=AlwaysHeldOperationLock(),
        )


def test_cli_defaults_to_dry_run_without_starting_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _write_authorization_files(tmp_path)
    original_load = executor.load_authorization

    def fixed_time_load(
        authorization_paths: executor.AuthorizationPaths,
        *,
        execute_live: bool,
        signature_verifier=None,
        operation_lock=None,
    ) -> executor.CanaryAuthorization:
        return original_load(
            authorization_paths,
            execute_live=execute_live,
            signature_verifier=signature_verifier,
            operation_lock=operation_lock,
            now=NOW,
        )

    original_executor = executor.AccountALiveTradeExecutor

    def fixed_time_executor(**kwargs):
        kwargs["clock"] = lambda: NOW
        kwargs["sleeper"] = lambda _seconds: None
        return original_executor(**kwargs)

    def reject_subprocess(*_args, **_kwargs):
        raise AssertionError("dry-run attempted to start a subprocess")

    monkeypatch.setattr(executor, "load_authorization", fixed_time_load)
    monkeypatch.setattr(
        executor,
        "AccountALiveTradeExecutor",
        fixed_time_executor,
    )
    monkeypatch.setattr(executor.subprocess, "run", reject_subprocess)
    evidence_path = tmp_path / "cli-evidence.json"

    exit_code = executor.main(
        [
            "--release-gate",
            str(paths.release_gate.payload),
            "--safety-gate",
            str(paths.safety_gate.payload),
            "--emergency-close-gate",
            str(paths.emergency_close_gate.payload),
            "--permit",
            str(paths.permit.payload),
            "--permit-ledger",
            str(tmp_path / "cli-ledger.json"),
            "--evidence-output",
            str(evidence_path),
        ]
    )

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["mode"] == "dry-run"
    assert summary["passed"] is True
    evidence = json.loads(evidence_path.read_text(encoding="ascii"))
    assert evidence["mode"] == "dry-run"
    assert "dry_run_round_trip" in evidence


def test_live_cli_rejects_nonfixed_ledger_before_operation_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _write_authorization_files(tmp_path, signed=True)
    adapter_path = _write_live_adapter(tmp_path)
    assert paths.reviewer_public_key is not None
    lock_constructed = False

    def reject_operation_lock_construction():
        nonlocal lock_constructed
        lock_constructed = True
        raise AssertionError(
            "nonfixed live ledger reached operation lock"
        )

    monkeypatch.setattr(
        executor,
        "LiveOperationLock",
        reject_operation_lock_construction,
    )

    exit_code = executor.main(
        [
            "--release-gate",
            str(paths.release_gate.payload),
            "--release-gate-signature",
            str(paths.release_gate.signature),
            "--safety-gate",
            str(paths.safety_gate.payload),
            "--safety-gate-signature",
            str(paths.safety_gate.signature),
            "--emergency-close-gate",
            str(paths.emergency_close_gate.payload),
            "--emergency-close-gate-signature",
            str(paths.emergency_close_gate.signature),
            "--permit",
            str(paths.permit.payload),
            "--permit-signature",
            str(paths.permit.signature),
            "--reviewer-public-key",
            str(paths.reviewer_public_key),
            "--permit-ledger",
            str(tmp_path / "replaceable-live-ledger.json"),
            "--evidence-output",
            str(tmp_path / "live-cli-evidence.json"),
            "--execute-live",
            "--live-adapter",
            str(adapter_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "live permit ledger path is fixed" in captured.err
    assert lock_constructed is False


def _authorization(tmp_path: Path) -> executor.CanaryAuthorization:
    paths = _write_authorization_files(tmp_path)
    authorization = executor.load_authorization(
        paths,
        execute_live=False,
        now=NOW,
    )
    return executor.CanaryAuthorization(
        **{
            **authorization.__dict__,
            "signatures_verified": True,
        }
    )


def _operation_lock(
    tmp_path: Path,
) -> executor.LiveOperationLock:
    return executor.LiveOperationLock(
        (tmp_path / "account-stall-operation.lock").resolve()
    )


def _write_live_adapter(
    tmp_path: Path,
) -> Path:
    adapter_path = (tmp_path / "reviewed-live-adapter").resolve()
    adapter_path.write_bytes(LIVE_ADAPTER_BYTES)
    adapter_path.chmod(0o500)
    return adapter_path


def _permit_store(
    path: Path,
) -> executor.SingleUsePermitStore:
    return executor.SingleUsePermitStore(
        path.resolve(),
        testing_allow_live_path_override=True,
        clock=lambda: NOW,
    )


def _seed_open_submitted_journal(
    store: executor.SingleUsePermitStore,
    authorization: executor.CanaryAuthorization,
) -> None:
    request = {
        **_base_request(authorization),
        "client_order_id": authorization.open_client_order_id,
        "quantity": str(authorization.quantity),
    }
    store.claim(authorization, mode="live")
    store.prepare_action(
        authorization,
        mode="live",
        action="OPEN",
        payload=request,
    )
    store.complete_action(
        authorization,
        mode="live",
        action="OPEN",
        state="OPEN_SUBMITTED",
        result={
            "adapter_evidence_sha256": _digest("seed-open"),
            "client_order_id": authorization.open_client_order_id,
        },
    )


def _assert_single_side_effect_identity(
    authorization: executor.CanaryAuthorization,
    *adapters: FakeAdapter,
) -> None:
    action_names = {
        "resume": "RESUME",
        "open": "OPEN",
        "cancel-open": "CANCEL_OPEN",
        "close": "CLOSE",
        "halt": "HALT",
    }
    for adapter_action, identity_action in action_names.items():
        action_requests: list[dict[str, Any]] = []
        for adapter in adapters:
            action_requests.extend(
                adapter.requests.get(adapter_action, [])
            )
        if not action_requests:
            continue
        expected = executor.deterministic_side_effect_id(
            authorization,
            identity_action,
        )
        side_effect_ids = {
            request["side_effect_id"]
            for request in action_requests
        }
        assert side_effect_ids == {expected}


def _inject_journal_fsync_failure(
    monkeypatch: pytest.MonkeyPatch,
    *,
    action: str,
    phase: str,
) -> list[str]:
    original_atomic_write_json = executor._atomic_write_json
    failures: list[str] = []

    def failing_atomic_write_json(
        path: Path,
        payload: Mapping[str, Any],
        *,
        mode: int,
    ) -> None:
        records = payload.get("records")
        if isinstance(records, dict) and not failures:
            for record in records.values():
                if not isinstance(record, dict):
                    continue
                history = record.get("history")
                if not isinstance(history, list) or not history:
                    continue
                latest = history[-1]
                if not isinstance(latest, dict):
                    continue
                event_type = str(latest.get("event_type") or "")
                event_payload = latest.get("payload")
                if not isinstance(event_payload, dict):
                    continue
                event_action = str(
                    event_payload.get("action") or ""
                )
                is_prepare = event_type == "ACTION_PENDING"
                matches_phase = phase == "prepare" and is_prepare
                if phase == "complete":
                    matches_phase = not is_prepare
                if event_action != action or not matches_phase:
                    continue
                failures.append(f"{phase}:{action}")
                raise OSError(
                    errno.ENOSPC,
                    os.strerror(errno.ENOSPC),
                )
        original_atomic_write_json(path, payload, mode=mode)

    monkeypatch.setattr(
        executor,
        "_atomic_write_json",
        failing_atomic_write_json,
    )
    return failures


def _executor(
    tmp_path: Path,
    authorization: executor.CanaryAuthorization,
    adapter: FakeAdapter,
    *,
    evidence_path: Path,
    permit_store: executor.SingleUsePermitStore | None = None,
    evidence_writer: executor.AtomicEvidenceWriter | None = None,
    clock: Callable[[], datetime] = lambda: NOW,
    recovery_max_attempts: int = 3,
    recovery_deadline_seconds: float = 20,
    journal_write_timeout_seconds: float = 1,
    monotonic: Callable[[], float] = lambda: 0.0,
) -> executor.AccountALiveTradeExecutor:
    selected_store = permit_store
    if selected_store is None:
        selected_store = _permit_store(
            tmp_path / f"{evidence_path.stem}-ledger.json"
        )
    selected_writer = evidence_writer
    if selected_writer is None:
        selected_writer = executor.AtomicEvidenceWriter(evidence_path)
    return executor.AccountALiveTradeExecutor(
        adapter=adapter,
        permit_store=selected_store,
        evidence_writer=selected_writer,
        operation_lock=AlwaysHeldOperationLock(),
        mode="live",
        max_observations=3,
        poll_interval_seconds=0,
        recovery_max_attempts=recovery_max_attempts,
        recovery_deadline_seconds=recovery_deadline_seconds,
        journal_write_timeout_seconds=(
            journal_write_timeout_seconds
        ),
        clock=clock,
        monotonic=monotonic,
        sleeper=lambda _seconds: None,
    )


def _write_authorization_files(
    root: Path,
    *,
    mutate=None,
    rebuild_hash_chain: bool = False,
    signed: bool = False,
) -> executor.AuthorizationPaths:
    documents = _documents()
    if mutate is not None:
        mutate(documents)
    if rebuild_hash_chain:
        _rebuild_hash_chain(documents)
    paths: dict[str, executor.SignedDocumentPaths] = {}
    for name, payload in documents.items():
        payload_path = root / f"{name}.json"
        payload_bytes = _json_bytes(payload)
        payload_path.write_bytes(payload_bytes)
        signature_path = None
        if signed:
            signature_path = root / f"{name}.sig"
            signature_path.write_bytes(
                hashlib.sha256(payload_bytes).hexdigest().encode(
                    "ascii"
                )
            )
        paths[name] = executor.SignedDocumentPaths(
            payload_path,
            signature_path,
        )
    public_key_path = None
    if signed:
        public_key_path = root / "reviewer.pem"
        public_key_path.write_bytes(PINNED_REVIEWER_PUBLIC_KEY)
        public_key_path.chmod(0o400)
    return executor.AuthorizationPaths(
        release_gate=paths["release_gate"],
        safety_gate=paths["safety_gate"],
        emergency_close_gate=paths["emergency_close_gate"],
        permit=paths["permit"],
        reviewer_public_key=public_key_path,
    )


def _documents() -> dict[str, dict[str, Any]]:
    permit_id = str(uuid4())
    intent_id = str(uuid4())
    release_identity = {
        "account_id": executor.ACCOUNT_ID,
        "symbol": executor.SYMBOL,
        "release_id": "release-account-a-canary",
        "image_digest": "sha256:" + ("1" * 64),
        "config_sha256": "2" * 64,
        "dependency_lock_sha256": "3" * 64,
        "live_adapter_sha256": LIVE_ADAPTER_SHA256,
        "permit_store_id": executor.LIVE_PERMIT_STORE_ID,
        "permit_store_path": str(
            executor.DEFAULT_LIVE_PERMIT_LEDGER_PATH
        ),
    }
    window = {
        "issued_at": "2026-08-08T11:00:00+00:00",
        "expires_at": "2026-08-08T13:00:00+00:00",
    }
    release_gate = {
        "schema_version": executor.RELEASE_GATE_SCHEMA,
        **release_identity,
        "rollout_phase": "account_a_canary",
        **window,
    }
    release_hash = _digest_json(release_gate)
    safety_gate = {
        "schema_version": executor.SAFETY_GATE_SCHEMA,
        **release_identity,
        "release_gate_sha256": release_hash,
        "canary_halted": True,
        "readiness_healthy": True,
        "reconciliation_healthy": True,
        "target_symbol_flat": True,
        "target_symbol_regular_orders_zero": True,
        "target_symbol_algo_orders_zero": True,
        "loss_monitor_healthy": True,
        "no_open_p0_p1_incidents": True,
        "non_target_portfolio_baseline_sha256": "4" * 64,
        "actor_tick_at": NOW.isoformat(),
        "heartbeat_at": NOW.isoformat(),
        "projection_at": NOW.isoformat(),
        "reconciliation_at": NOW.isoformat(),
        "loss_monitor_at": NOW.isoformat(),
        "health_max_age_seconds": "5",
        **window,
    }
    safety_hash = _digest_json(safety_gate)
    emergency_gate = {
        "schema_version": executor.EMERGENCY_CLOSE_GATE_SCHEMA,
        **release_identity,
        "release_gate_sha256": release_hash,
        "safety_gate_sha256": safety_hash,
        "verified": True,
        "environment": "testnet",
        "open_order_type": "LIMIT",
        "open_time_in_force": "IOC",
        "close_order_type": "MARKET",
        "close_reduce_only": True,
        "verified_quantity": "0.1",
        "testnet_evidence_sha256": "5" * 64,
        "verified_at": "2026-08-08T11:30:00+00:00",
        "target_symbol_flat": True,
        "target_symbol_regular_orders_zero": True,
        "target_symbol_algo_orders_zero": True,
        **window,
    }
    emergency_hash = _digest_json(emergency_gate)
    permit = {
        "schema_version": executor.PERMIT_SCHEMA,
        **release_identity,
        "permit_id": permit_id,
        "intent_id": intent_id,
        "open_client_order_id": (
            executor.deterministic_open_client_order_id(intent_id)
        ),
        "close_client_order_id": (
            executor.deterministic_close_client_order_id(intent_id)
        ),
        "open_side": "BUY",
        "quantity": "0.1",
        "limit_price_usdt": "100",
        "max_notional_usdt": "12",
        "max_cumulative_net_loss_usdt": "1.49",
        "max_round_trips": 1,
        "single_use": True,
        "portfolio_baseline_sha256": "4" * 64,
        "release_gate_sha256": release_hash,
        "safety_gate_sha256": safety_hash,
        "emergency_close_gate_sha256": emergency_hash,
        **window,
    }
    return {
        "release_gate": release_gate,
        "safety_gate": safety_gate,
        "emergency_close_gate": emergency_gate,
        "permit": permit,
    }


def _rebuild_hash_chain(
    documents: dict[str, dict[str, Any]],
) -> None:
    release_hash = _digest_json(documents["release_gate"])
    documents["safety_gate"]["release_gate_sha256"] = release_hash
    safety_hash = _digest_json(documents["safety_gate"])
    documents["emergency_close_gate"][
        "release_gate_sha256"
    ] = release_hash
    documents["emergency_close_gate"][
        "safety_gate_sha256"
    ] = safety_hash
    emergency_hash = _digest_json(
        documents["emergency_close_gate"]
    )
    documents["permit"]["release_gate_sha256"] = release_hash
    documents["permit"]["safety_gate_sha256"] = safety_hash
    documents["permit"][
        "emergency_close_gate_sha256"
    ] = emergency_hash


def _base_request(
    authorization: executor.CanaryAuthorization,
) -> dict[str, Any]:
    return {
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
    }


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _digest_json(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json_bytes(payload)).hexdigest()


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()
