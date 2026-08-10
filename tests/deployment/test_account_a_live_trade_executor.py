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
from collections.abc import Callable, Mapping, Sequence
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
LIVE_EXECUTOR_SHA256 = hashlib.sha256(
    Path(executor.__file__).read_bytes()
).hexdigest()
PINNED_REVIEWER_PUBLIC_KEY = b"""-----BEGIN PUBLIC KEY-----
MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEA6dzynRm7dbxyhsiAMWmq
M8SBjMA5qSMfG80dtMFliDXUnooAmA0vO+K9enkiyA2naiACf8r7LAObz6N+Iu8H
uzXS1Dv1BSFjRatQlaNMMM1x8VKCU05MROm2zTzeHr1TqHSjyQsGErjNRJEXKaK6
/hgDmUn6OAlyCMDX6+3wog6nEwnhQlEPGzYZmy+W9qGg4vFFRsQKHWJ0tb1Kwocf
OzSeYxMY16Buq+PnIzOD1ZUqQDXXgJRIMQ1f2pwW9niYYkMQ+oETI14dqaQHjCll
CHLNwa3GLchR/2D8wbUzUsaB+hU6xadKcecymHIh7dKZU+sbrN4q2YbuE6IT1fq5
S5k2gzyl9smIoMX67Iunef5MJf0l2Va0aR+/HTmS3xOEUlNPbI/gOMAI8zE3LHBK
Tf9DpAl32exs2PGOo6bWedHZEJA+9fRH0UG3VOTxJRGZAuoL23KqzlbJNe2PC6df
Vc4YzmH7pnCuRBhEMepzeg8dycZH+RYgFbHxvicfLJwDQPKWq9rJEL/FCrYlNsE9
Ile9uAfVbg0VrfCwsBTC/6BpD4aXB/oHdfIhnpOdh5XXDIiLDSw34zkLZoMllaoe
feedZq6dZMqJn7lJrtJxtZmtOFXep965RJuSkHeqFbcisD2uiIumOMLLeZdTi1Jy
4Ct0Urh8pZIdWMJoc0GRta0CAwEAAQ==
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
        replace_existing: bool = False,
    ) -> str:
        if self.crash_pending:
            self.crash_pending = False
            raise InjectedCrash("crash before evidence publication")
        return super().commit_prepared(
            evidence,
            evidence_sha256=evidence_sha256,
            allow_existing=allow_existing,
            replace_existing=replace_existing,
        )


class CrashBeforeReplaceEvidenceWriter(executor.AtomicEvidenceWriter):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.crash_pending = True

    def commit_prepared(
        self,
        evidence: Mapping[str, Any],
        *,
        evidence_sha256: str,
        allow_existing: bool,
        replace_existing: bool = False,
    ) -> str:
        if replace_existing and self.crash_pending:
            self.crash_pending = False
            raise InjectedCrash("before recoverable evidence replacement")
        return super().commit_prepared(
            evidence,
            evidence_sha256=evidence_sha256,
            allow_existing=allow_existing,
            replace_existing=replace_existing,
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
        replace_existing: bool = False,
    ) -> str:
        self.operation_lock.require_held()
        return super().commit_prepared(
            evidence,
            evidence_sha256=evidence_sha256,
            allow_existing=allow_existing,
            replace_existing=replace_existing,
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
        loss_monitor_source: str = "exchange_mirror+node",
        exchange_evidence_state: str = "confirmed_executed",
        preflight_failures: int = 0,
        preflight_position_quantity: str = "0",
        preflight_position_side: str = "FLAT",
        preflight_regular_order_count: int = 0,
        preflight_algo_order_count: int = 0,
        preflight_baseline_sha256: str = "",
        preflight_available_usdt_balance: str | bool = "100",
        preflight_fetched_at: datetime = NOW,
        refresh_preflight_after_resume: bool = True,
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
        resume_observed_at: datetime = NOW,
        on_resume: Callable[[], None] | None = None,
        resume_failures: int = 0,
        observe_failures: int = 0,
        open_error_after_effect: BaseException | None = None,
        close_error_after_effect: BaseException | None = None,
        final_failures: int = 0,
        post_halt_final_failures: int = 0,
        post_halt_final_fetched_at: datetime | None = None,
        final_enrichment_degraded: bool = False,
        final_warnings: Sequence[str] | None = None,
        final_baseline_sha256: str = "",
        final_open_filled_quantity: str = "0.07",
        final_open_average_fill_price_usdt: str = "",
        close_enrichment_degraded: bool = False,
        close_warnings: Sequence[str] | None = None,
        open_fill_attempts: Sequence[bool] | None = None,
        operation_lock: executor.LiveOperationLock | None = None,
    ) -> None:
        self.authorization = authorization
        self.fail_observe = fail_observe
        self.observation_loss = observation_loss
        self.observation_price = observation_price
        self.observation_status = observation_status
        self.observation_times = dict(observation_times or {})
        self.loss_monitor_source = loss_monitor_source
        self.exchange_evidence_state = exchange_evidence_state
        self.preflight_failures = preflight_failures
        self.preflight_position_quantity = preflight_position_quantity
        self.preflight_position_side = preflight_position_side
        self.preflight_regular_order_count = preflight_regular_order_count
        self.preflight_algo_order_count = preflight_algo_order_count
        self.preflight_baseline_sha256 = preflight_baseline_sha256
        self.preflight_available_usdt_balance = (
            preflight_available_usdt_balance
        )
        self.preflight_fetched_at = preflight_fetched_at
        self.refresh_preflight_after_resume = (
            refresh_preflight_after_resume
        )
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
        self.resume_observed_at = resume_observed_at
        self.on_resume = on_resume
        self.resume_failures = resume_failures
        self.observe_failures = observe_failures
        self.open_error_after_effect = open_error_after_effect
        self.close_error_after_effect = close_error_after_effect
        self.final_failures = final_failures
        self.post_halt_final_failures = post_halt_final_failures
        self.post_halt_final_fetched_at = post_halt_final_fetched_at
        self.final_enrichment_degraded = final_enrichment_degraded
        self.final_warnings = tuple(final_warnings or ())
        self.final_baseline_sha256 = final_baseline_sha256
        self.final_open_filled_quantity = final_open_filled_quantity
        final_open_price = final_open_average_fill_price_usdt
        if not final_open_price:
            final_open_price = observation_price
        self.final_open_average_fill_price_usdt = final_open_price
        self.close_enrichment_degraded = close_enrichment_degraded
        self.close_warnings = tuple(close_warnings or ())
        self.open_fill_attempts = tuple(open_fill_attempts or ())
        self.operation_lock = operation_lock
        self.calls: list[str] = []
        self.requests: dict[str, list[dict[str, Any]]] = {}
        self.close_requests: list[Mapping[str, Any]] = []
        self.close_effect_quantities: list[Decimal] = []
        self.close_acks: dict[str, Mapping[str, Any]] = {}
        self.position_quantity = Decimal(0)
        self.position_side = "FLAT"
        self.trading_state = "HALTED"

    def preflight(
        self,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self._require_operation_lock()
        self._record_call("preflight", request)
        if self.calls.count("preflight") <= self.preflight_failures:
            raise TimeoutError("injected preflight response timeout")
        baseline = self.preflight_baseline_sha256
        if not baseline:
            baseline = self.authorization.portfolio_baseline_sha256
        payload = self._identity()
        payload.update(
            {
                "action": "preflight",
                "source": executor.EXCHANGE_EVIDENCE_SOURCE,
                "exchange_authoritative": True,
                "fetched_at": self.preflight_fetched_at.isoformat(),
                "mirror_stale": False,
                "target_position_side": self.preflight_position_side,
                "target_position_quantity": (
                    self.preflight_position_quantity
                ),
                "target_regular_order_count": (
                    self.preflight_regular_order_count
                ),
                "target_algo_order_count": (
                    self.preflight_algo_order_count
                ),
                "non_target_portfolio_baseline_sha256": baseline,
                "node_snapshot": {
                    "account_id": self.authorization.account_id,
                    "node_id": self.authorization.release.node_id,
                    "writer_id": self.authorization.release.writer_id,
                    "lease_id": self.authorization.release.lease_id,
                    "fencing_epoch": (
                        self.authorization.release.fencing_epoch
                    ),
                    "trading_state": self.trading_state,
                    "process_liveness": True,
                    "actor_tick_at": (
                        self.authorization.actor_tick_at.isoformat()
                    ),
                    "loss_monitor_healthy": True,
                    "loss_monitor_at": (
                        self.authorization.loss_monitor_at.isoformat()
                    ),
                },
                "evidence_sha256": _digest("preflight"),
            }
        )
        if self.preflight_available_usdt_balance is not False:
            payload["available_usdt_balance"] = (
                self.preflight_available_usdt_balance
            )
        return payload

    def resume(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self._require_operation_lock()
        self._record_call("resume", request)
        if self.calls.count("resume") <= self.resume_failures:
            raise TimeoutError("injected RESUME response timeout")
        self.trading_state = "ACTIVE"
        if self.refresh_preflight_after_resume:
            self.preflight_fetched_at = max(
                self.preflight_fetched_at,
                self.resume_observed_at,
            )
        if self.on_resume is not None:
            self.on_resume()
        return self._ack(
            "resume",
            source=executor.NODE_STATE_EVIDENCE_SOURCE,
            trading_state=self.trading_state,
            observed_at=self.resume_observed_at.isoformat(),
            side_effect_id=request["side_effect_id"],
        )

    def submit_open(
        self,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self._require_operation_lock()
        self._record_call("open", request)
        open_attempt = self.calls.count("open")
        should_fill = True
        if open_attempt <= len(self.open_fill_attempts):
            should_fill = self.open_fill_attempts[open_attempt - 1]
        if should_fill:
            self.position_quantity = self.authorization.quantity
            self.position_side = "LONG"
            if self.authorization.open_side == "SELL":
                self.position_side = "SHORT"
        else:
            self.position_quantity = Decimal(0)
            self.position_side = "FLAT"
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
        open_status = self.observation_status
        exchange_evidence_state = self.exchange_evidence_state
        if self.position_quantity == 0 and self.open_fill_attempts:
            open_status = "EXPIRED"
            if exchange_evidence_state == "confirmed_executed":
                exchange_evidence_state = "definitively_absent"
        payload = self._identity()
        payload.update(
            {
                "open_client_order_id": (
                    self.authorization.open_client_order_id
                ),
                "open_status": open_status,
                "filled_quantity": str(self.position_quantity),
                "average_fill_price_usdt": self.observation_price,
                "cumulative_net_loss_usdt": self.observation_loss,
                "mark_fresh": True,
                "loss_monitor_healthy": True,
                "observed_at": observed_at.isoformat(),
                "mark_at": mark_at.isoformat(),
                "loss_monitor_at": loss_monitor_at.isoformat(),
                "loss_monitor_source": self.loss_monitor_source,
                "exchange_evidence_state": exchange_evidence_state,
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
        side_effect_id = str(request["side_effect_id"])
        cached_ack = self.close_acks.get(side_effect_id)
        if cached_ack is not None:
            return dict(cached_ack)
        if self.calls.count("close") <= self.close_failures:
            raise RuntimeError("injected close failure")
        filled_quantity = str(request["quantity"])
        self.close_effect_quantities.append(Decimal(filled_quantity))
        self.position_quantity = max(
            Decimal(0),
            self.position_quantity - Decimal(filled_quantity),
        )
        if self.position_quantity == 0:
            self.position_side = "FLAT"
        payload = self._ack(
            "close",
            client_order_id=(
                self.authorization.close_client_order_id
            ),
            filled_quantity=filled_quantity,
            intent_id=self.authorization.close_intent_id,
            side_effect_id=request["side_effect_id"],
        )
        payload["enrichment_degraded"] = (
            self.close_enrichment_degraded
        )
        payload["warnings"] = list(self.close_warnings)
        self.close_acks[side_effect_id] = dict(payload)
        error = self.close_error_after_effect
        if error is not None:
            self.close_error_after_effect = None
            raise error
        return payload

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
        baseline = self.final_baseline_sha256
        if not baseline:
            baseline = self.authorization.portfolio_baseline_sha256
        payload = self._identity()
        payload.update(
            {
                "target_symbol_flat": self.position_quantity == 0,
                "target_symbol_regular_orders_zero": True,
                "target_symbol_algo_orders_zero": True,
                "position_quantity": str(self.position_quantity),
                "non_target_portfolio_baseline_sha256": baseline,
                "open_filled_quantity": self.final_open_filled_quantity,
                "open_average_fill_price_usdt": (
                    self.final_open_average_fill_price_usdt
                ),
                "gross_pnl_usdt": str(gross_pnl),
                "fees_usdt": str(fees),
                "net_pnl_usdt": str(net_pnl),
                "cumulative_net_loss_usdt": str(
                    cumulative_loss
                ),
                "source": self.final_source,
                "fetched_at": fetched_at.isoformat(),
                "enrichment_degraded": self.final_enrichment_degraded,
                "financial_proof_complete": (
                    not self.final_enrichment_degraded
                ),
                "warnings": list(self.final_warnings),
                "evidence_sha256": _digest(evidence_label),
            }
        )
        return payload

    def halt(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self._require_operation_lock()
        self._record_call("halt", request)
        if self.calls.count("halt") <= self.halt_failures:
            raise RuntimeError("injected HALT failure")
        self.trading_state = self.halt_state
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


def test_live_authorization_allows_expired_documents_for_evidence_recovery(
    tmp_path: Path,
) -> None:
    verifier = FakeSignatureVerifier()
    paths = _write_authorization_files(
        tmp_path,
        signed=True,
    )
    expired_now = NOW + timedelta(hours=2)

    with _operation_lock(tmp_path) as operation_lock:
        authorization = executor.load_authorization(
            paths,
            execute_live=True,
            signature_verifier=verifier,
            operation_lock=operation_lock,
            now=expired_now,
            allow_expired=True,
        )

    assert authorization.expires_at < expired_now
    assert authorization.signatures_verified is True
    assert any(
        "EVIDENCE_RECOVERY_EXPIRED_AUTHORIZATION" in warning
        for warning in authorization.warnings
    )


def test_evidence_recovery_gate_rejects_symlinked_output_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_path = (tmp_path / "protected-ledger.json").resolve()
    ledger_path.write_bytes(b'{"sentinel":"unchanged"}\n')
    monkeypatch.setattr(
        executor,
        "DEFAULT_LIVE_PERMIT_LEDGER_PATH",
        ledger_path,
    )
    authorization = _authorization(tmp_path)
    authorization = replace(
        authorization,
        release=replace(
            authorization.release,
            permit_store_path=str(ledger_path),
        ),
    )
    real_parent = tmp_path / "real-evidence-parent"
    real_parent.mkdir(mode=0o700)
    symlink_parent = tmp_path / "linked-evidence-parent"
    symlink_parent.symlink_to(real_parent, target_is_directory=True)
    evidence_path = symlink_parent / "live-evidence.json"
    recovery_gate = _write_evidence_recovery_gate(
        tmp_path,
        authorization,
        ledger_path=ledger_path,
        evidence_path=evidence_path,
    )
    assert authorization.signatures_verified is True
    reviewer_public_key = tmp_path / "recovery-reviewer.pem"
    reviewer_public_key.write_bytes(PINNED_REVIEWER_PUBLIC_KEY)
    reviewer_public_key.chmod(0o400)
    before = ledger_path.read_bytes()

    with pytest.raises(
        executor.LiveTradeExecutionError,
        match="symlink",
    ):
        executor.load_evidence_recovery_gate(
            recovery_gate,
            reviewer_public_key=reviewer_public_key,
            authorization=authorization,
            evidence_path=evidence_path,
            signature_verifier=FakeSignatureVerifier(),
            operation_lock=AlwaysHeldOperationLock(),
            now=NOW,
        )

    assert ledger_path.read_bytes() == before
    assert list(real_parent.iterdir()) == []


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
def test_authorization_accepts_stale_signed_safety_timestamp_with_warning(
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


def test_authorization_accepts_120_second_exchange_window(
    tmp_path: Path,
) -> None:
    paths = _write_authorization_files(
        tmp_path,
        mutate=lambda documents: documents["safety_gate"].update(
            {"exchange_max_age_seconds": "120"}
        ),
        rebuild_hash_chain=True,
    )

    authorization = executor.load_authorization(
        paths,
        execute_live=False,
        now=NOW,
    )

    assert authorization.exchange_max_age_seconds == Decimal(120)


def test_authorization_rejects_exchange_window_over_180_seconds(
    tmp_path: Path,
) -> None:
    paths = _write_authorization_files(
        tmp_path,
        mutate=lambda documents: documents["safety_gate"].update(
            {"exchange_max_age_seconds": "181"}
        ),
        rebuild_hash_chain=True,
    )

    with pytest.raises(
        executor.LiveTradeExecutionError,
        match="exchange max age exceeds 180 seconds",
    ):
        executor.load_authorization(
            paths,
            execute_live=False,
            now=NOW,
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


@pytest.mark.parametrize(
    "field_name",
    [
        "ownership_healthy",
        "fencing_healthy",
        "durability_healthy",
    ],
)
def test_authorization_rejects_explicit_scoped_safety_failure(
    tmp_path: Path,
    field_name: str,
) -> None:
    paths = _write_authorization_files(
        tmp_path,
        mutate=lambda documents: documents["safety_gate"].update(
            {field_name: False}
        ),
        rebuild_hash_chain=True,
    )

    with pytest.raises(
        executor.LiveTradeExecutionError,
        match=f"safety gate requires {field_name}=true",
    ):
        executor.load_authorization(
            paths,
            execute_live=False,
            now=NOW,
        )


@pytest.mark.parametrize(
    ("field_name", "warning_code"),
    [
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
    ],
)
def test_authorization_treats_missing_scoped_safety_health_as_advisory(
    tmp_path: Path,
    field_name: str,
    warning_code: str,
) -> None:
    paths = _write_authorization_files(
        tmp_path,
        mutate=lambda documents: documents["safety_gate"].pop(
            field_name
        ),
        rebuild_hash_chain=True,
    )

    authorization = executor.load_authorization(
        paths,
        execute_live=False,
        now=NOW,
    )

    assert any(
        warning_code in warning
        for warning in authorization.warnings
    )


@pytest.mark.parametrize("field_value", [None, False])
def test_authorization_treats_risk_health_as_advisory(
    tmp_path: Path,
    field_value: bool | None,
) -> None:
    def mutate(documents: dict[str, dict[str, Any]]) -> None:
        safety_gate = documents["safety_gate"]
        if field_value is None:
            safety_gate.pop("risk_healthy")
            return
        safety_gate["risk_healthy"] = field_value

    paths = _write_authorization_files(
        tmp_path,
        mutate=mutate,
        rebuild_hash_chain=True,
    )

    authorization = executor.load_authorization(
        paths,
        execute_live=False,
        now=NOW,
    )

    assert any(
        "SAFETY_RISK_DEGRADED" in warning
        for warning in authorization.warnings
    )


def test_authorization_treats_missing_loss_monitor_health_as_advisory(
    tmp_path: Path,
) -> None:
    paths = _write_authorization_files(
        tmp_path,
        mutate=lambda documents: documents["safety_gate"].pop(
            "loss_monitor_healthy"
        ),
        rebuild_hash_chain=True,
    )

    authorization = executor.load_authorization(
        paths,
        execute_live=False,
        now=NOW,
    )

    assert any(
        "SAFETY_LOSS_MONITOR_HEALTH_MISSING" in warning
        for warning in authorization.warnings
    )


def test_authorization_treats_missing_process_liveness_as_advisory(
    tmp_path: Path,
) -> None:
    paths = _write_authorization_files(
        tmp_path,
        mutate=lambda documents: documents["safety_gate"].pop(
            "process_liveness",
            None,
        ),
        rebuild_hash_chain=True,
    )

    authorization = executor.load_authorization(
        paths,
        execute_live=False,
        now=NOW,
    )

    assert any(
        "SAFETY_PROCESS_LIVENESS_MISSING" in warning
        for warning in authorization.warnings
    )


def test_authorization_rejects_explicit_unhealthy_loss_monitor(
    tmp_path: Path,
) -> None:
    paths = _write_authorization_files(
        tmp_path,
        mutate=lambda documents: documents["safety_gate"].update(
            {"loss_monitor_healthy": False}
        ),
        rebuild_hash_chain=True,
    )

    with pytest.raises(
        executor.LiveTradeExecutionError,
        match="safety gate requires loss_monitor_healthy=true",
    ):
        executor.load_authorization(
            paths,
            execute_live=False,
            now=NOW,
        )


def test_authorization_rejects_explicit_dead_process(
    tmp_path: Path,
) -> None:
    paths = _write_authorization_files(
        tmp_path,
        mutate=lambda documents: documents["safety_gate"].update(
            {"process_liveness": False}
        ),
        rebuild_hash_chain=True,
    )

    with pytest.raises(
        executor.LiveTradeExecutionError,
        match="safety gate requires process_liveness=true",
    ):
        executor.load_authorization(
            paths,
            execute_live=False,
            now=NOW,
        )


@pytest.mark.parametrize("field_name", executor.SIGNED_HEALTH_FIELDS)
def test_authorization_treats_missing_health_timestamp_as_advisory(
    tmp_path: Path,
    field_name: str,
) -> None:
    paths = _write_authorization_files(
        tmp_path,
        mutate=lambda documents: documents["safety_gate"].pop(
            field_name
        ),
        rebuild_hash_chain=True,
    )

    authorization = executor.load_authorization(
        paths,
        execute_live=False,
        now=NOW,
    )

    assert getattr(authorization, field_name) is None
    assert any(
        (
            "SAFETY_HEALTH_TELEMETRY_MISSING" in warning
            and field_name in warning
        )
        for warning in authorization.warnings
    )


def test_authorization_parses_fee_reserve_and_optional_exchange_filters(
    tmp_path: Path,
) -> None:
    def mutate(documents: dict[str, dict[str, Any]]) -> None:
        documents["permit"]["exchange_filters"] = {
            "quantity_step": "0.01",
            "min_quantity": "0.01",
            "price_tick": "0.01",
            "min_notional": "5",
        }

    paths = _write_authorization_files(
        tmp_path,
        mutate=mutate,
        rebuild_hash_chain=True,
    )

    authorization = executor.load_authorization(
        paths,
        execute_live=False,
        now=NOW,
    )

    assert authorization.fee_reserve_usdt == Decimal("0.10")
    assert authorization.exchange_filters == {
        "quantity_step": Decimal("0.01"),
        "min_quantity": Decimal("0.01"),
        "price_tick": Decimal("0.01"),
        "min_notional": Decimal("5"),
    }


def test_authorization_requires_fixed_live_canary_quantity(
    tmp_path: Path,
) -> None:
    def mutate(documents: dict[str, dict[str, Any]]) -> None:
        documents["emergency_close_gate"]["verified_quantity"] = "0.1"
        documents["permit"]["quantity"] = "0.1"

    paths = _write_authorization_files(
        tmp_path,
        mutate=mutate,
        rebuild_hash_chain=True,
    )

    with pytest.raises(
        executor.LiveTradeExecutionError,
        match="permit quantity must equal 0.07 SOL",
    ):
        executor.load_authorization(
            paths,
            execute_live=False,
            now=NOW,
        )


def test_authorization_missing_exchange_filters_is_advisory(
    tmp_path: Path,
) -> None:
    paths = _write_authorization_files(
        tmp_path,
        mutate=lambda documents: documents["permit"].pop(
            "exchange_filters"
        ),
        rebuild_hash_chain=True,
    )

    authorization = executor.load_authorization(
        paths,
        execute_live=False,
        now=NOW,
    )

    assert authorization.exchange_filters == {}
    assert any(
        "EXCHANGE_FILTERS_MISSING" in warning
        for warning in authorization.warnings
    )


def test_authorization_rejects_malformed_exchange_filters(
    tmp_path: Path,
) -> None:
    paths = _write_authorization_files(
        tmp_path,
        mutate=lambda documents: documents["permit"].update(
            {"exchange_filters": "unavailable"}
        ),
        rebuild_hash_chain=True,
    )

    with pytest.raises(
        executor.LiveTradeExecutionError,
        match="exchange_filters must be an object",
    ):
        executor.load_authorization(
            paths,
            execute_live=False,
            now=NOW,
        )


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("quantity_step", "0.03"),
        ("min_quantity", "0.11"),
        ("price_tick", "0.03"),
        ("min_notional", "11"),
    ],
)
def test_authorization_rejects_known_exchange_filter_violation(
    tmp_path: Path,
    field_name: str,
    field_value: str,
) -> None:
    def mutate(documents: dict[str, dict[str, Any]]) -> None:
        filters = {
            "quantity_step": "0.01",
            "min_quantity": "0.01",
            "price_tick": "0.01",
            "min_notional": "5",
        }
        filters[field_name] = field_value
        documents["permit"]["exchange_filters"] = filters

    paths = _write_authorization_files(
        tmp_path,
        mutate=mutate,
        rebuild_hash_chain=True,
    )

    with pytest.raises(
        executor.LiveTradeExecutionError,
        match=f"exchange filter rejected: {field_name}",
    ):
        executor.load_authorization(
            paths,
            execute_live=False,
            now=NOW,
        )


@pytest.mark.parametrize(
    "field_name",
    [
        "node_id",
        "writer_id",
        "lease_id",
        "fencing_epoch",
    ],
)
def test_authorization_requires_signed_execution_identity(
    tmp_path: Path,
    field_name: str,
) -> None:
    paths = _write_authorization_files(
        tmp_path,
        mutate=lambda documents: documents["release_gate"].pop(
            field_name
        ),
        rebuild_hash_chain=True,
    )

    with pytest.raises(
        executor.LiveTradeExecutionError,
        match=f"release gate {field_name}",
    ):
        executor.load_authorization(
            paths,
            execute_live=False,
            now=NOW,
        )


@pytest.mark.parametrize(
    "document_name",
    [
        "safety_gate",
        "emergency_close_gate",
        "permit",
    ],
)
@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("node_id", "nautilus-node-account-b"),
        ("writer_id", "writer-account-b"),
        ("lease_id", "lease-account-b"),
        ("fencing_epoch", 43),
    ],
)
def test_authorization_rejects_execution_identity_drift(
    tmp_path: Path,
    document_name: str,
    field_name: str,
    field_value: str | int,
) -> None:
    paths = _write_authorization_files(
        tmp_path,
        mutate=lambda documents: documents[document_name].update(
            {field_name: field_value}
        ),
        rebuild_hash_chain=True,
    )

    with pytest.raises(
        executor.LiveTradeExecutionError,
        match=(
            f"{document_name.replace('_', ' ')} "
            f"release identity mismatch: {field_name}"
        ),
    ):
        executor.load_authorization(
            paths,
            execute_live=False,
            now=NOW,
        )


def test_authorization_accepts_emergency_close_contract_replay_with_warning(
    tmp_path: Path,
) -> None:
    paths = _write_authorization_files(
        tmp_path,
        mutate=lambda documents: documents["emergency_close_gate"].update(
            {
                "evidence_type": "contract_replay",
                "environment": "contract_replay",
            }
        ),
        rebuild_hash_chain=True,
    )

    authorization = executor.load_authorization(
        paths,
        execute_live=False,
        now=NOW,
    )

    assert authorization.emergency_close_evidence_type == "contract_replay"
    assert authorization.emergency_close_environment == "contract_replay"
    assert (
        "EMERGENCY_CLOSE_CONTRACT_REPLAY_ONLY: "
        "emergency close evidence is a deterministic contract replay"
    ) in authorization.warnings


def test_authorization_rejects_unknown_emergency_close_evidence_type(
    tmp_path: Path,
) -> None:
    paths = _write_authorization_files(
        tmp_path,
        mutate=lambda documents: documents["emergency_close_gate"].update(
            {
                "evidence_type": "manual_claim",
                "environment": "manual",
            }
        ),
        rebuild_hash_chain=True,
    )

    with pytest.raises(
        executor.LiveTradeExecutionError,
        match="emergency close evidence_type is unsupported",
    ):
        executor.load_authorization(
            paths,
            execute_live=False,
            now=NOW,
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


def test_live_executor_accepts_only_signed_canonical_bytes(
    tmp_path: Path,
) -> None:
    executor_path = (tmp_path / "reviewed-live-executor").resolve()
    payload = Path(executor.__file__).read_bytes()
    executor_path.write_bytes(payload)
    executor_path.chmod(0o500)

    actual_sha256 = executor.validate_live_executor(
        executor_path,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )

    assert actual_sha256 == hashlib.sha256(payload).hexdigest()


def test_live_executor_rejects_hash_drift(
    tmp_path: Path,
) -> None:
    executor_path = (tmp_path / "reviewed-live-executor").resolve()
    executor_path.write_bytes(Path(executor.__file__).read_bytes())
    executor_path.chmod(0o500)

    with pytest.raises(
        executor.LiveTradeExecutionError,
        match="live executor hash differs from signed release",
    ):
        executor.validate_live_executor(
            executor_path,
            expected_sha256="f" * 64,
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


@pytest.mark.parametrize(
    ("method_name", "payload_fields", "expected_code"),
    [
        (
            "resume",
            {"error_class": "http_timeout"},
            "HTTP_TIMEOUT",
        ),
        (
            "submit_open",
            {"code": "HTTP_501"},
            "HTTP_5XX",
        ),
        (
            "observe",
            {"status_code": 599},
            "HTTP_5XX",
        ),
        (
            "cancel_open",
            {"code": 425},
            "HTTP_425",
        ),
        (
            "current_position",
            {"code": "HTTP_429"},
            "HTTP_429",
        ),
        (
            "submit_close",
            {"error_class": "circuit_open"},
            "CIRCUIT_OPEN",
        ),
        (
            "final_snapshot",
            {"code": "resource_pressure"},
            "RESOURCE_PRESSURE",
        ),
        (
            "halt",
            {"error_class": "reconciliation_pressure"},
            "RECONCILIATION_PRESSURE",
        ),
    ],
)
def test_json_adapter_raises_soft_failure_for_structured_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    payload_fields: Mapping[str, Any],
    expected_code: str,
) -> None:
    adapter_path = _write_live_adapter(tmp_path)
    validated = executor.validate_live_adapter(
        adapter_path,
        expected_sha256=LIVE_ADAPTER_SHA256,
    )
    payload = {
        "accepted": False,
        "reason": "temporary adapter pressure",
        **payload_fields,
    }

    def rejected_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=_json_bytes(payload),
            stderr=b"",
        )

    monkeypatch.setattr(executor.subprocess, "run", rejected_run)

    with executor.JsonCommandAdapter(
        validated,
        timeout_seconds=15,
        live_authorized=True,
        operation_lock=AlwaysHeldOperationLock(),
    ) as adapter, pytest.raises(
        executor.SoftActionFailure,
    ) as raised:
        method = getattr(adapter, method_name)
        method({})

    assert raised.value.code == expected_code


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
        "halt",
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
    assert result.close_quantity == Decimal("0.07")
    assert adapter.calls.index("halt") < adapter.calls.index("close")
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
            "quantity": "0.07",
            "reduce_only": True,
            "reason": "failure-cleanup",
            "attempt": 1,
                "side_effect_id": executor.deterministic_side_effect_id(
                    authorization,
                    "CLOSE",
                ),
                "hard_timeout_seconds": 20.0,
                "exchange_not_before": NOW.isoformat(),
            }
        ]
    evidence = json.loads(evidence_path.read_text(encoding="ascii"))
    assert evidence["passed"] is False
    assert evidence["finished_halted"] is True
    assert evidence["mainnet_round_trip"]["close_reduce_only"] is True
    assert evidence["mainnet_round_trip"]["close_quantity"] == "0.07"
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
    expected_halt_count = 1
    if expected_close_count == 1:
        expected_halt_count = 2
    assert adapter.calls.count("halt") == expected_halt_count
    if expected_close_count == 1:
        first_halt = adapter.calls.index("halt")
        last_halt = len(adapter.calls) - 1 - adapter.calls[::-1].index(
            "halt"
        )
        assert first_halt < adapter.calls.index("position")
        assert adapter.calls.index("position") < adapter.calls.index(
            "close"
        )
        assert adapter.calls.index("close") < last_halt
    else:
        assert "position" not in adapter.calls
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
        "preflight",
        "resume",
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

    assert result.status == "DEGRADED"
    assert result.passed is True
    assert result.error_code == "SOFT_TRANSPORT_DEGRADED"
    assert result.retryable is False
    assert result.failure_reason == ""
    assert any(
        "OPEN response unavailable" in reason
        for reason in result.degraded_reasons
    )
    assert adapter.calls.count("open") == 1
    assert adapter.calls == [
        "preflight",
        "resume",
        "preflight",
        "open",
        "observe",
        "halt",
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


def test_open_timeout_recovers_when_position_confirms_execution(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        open_error_after_effect=TimeoutError(
            "injected open response timeout"
        ),
        observe_failures=3,
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "open-timeout-position-proof.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert result.passed is True
    assert result.error_code == "SOFT_TRANSPORT_DEGRADED"
    assert result.failure_reason == ""
    assert adapter.calls.count("open") == 1
    assert adapter.calls.count("observe") == 3
    assert adapter.calls.count("position") == 2
    assert adapter.calls.count("close") == 1
    assert result.close_submitted is True
    assert any(
        "confirmed executed by exchange position" in reason
        for reason in result.degraded_reasons
    )


def test_open_timeout_stays_blocked_while_exchange_result_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        observe_failures=3,
    )

    def timeout_before_exchange_effect(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        adapter._require_operation_lock()
        adapter._record_call("open", request)
        raise TimeoutError("injected open response timeout")

    monkeypatch.setattr(
        adapter,
        "submit_open",
        timeout_before_exchange_effect,
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "open-timeout-unknown.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert result.passed is False
    assert result.error_code == "OPEN_RESULT_AMBIGUOUS"
    assert "exchange result remained unknown" in result.failure_reason
    assert adapter.calls.count("open") == 1
    assert adapter.calls.count("observe") == 3
    assert adapter.calls.count("close") == 0
    assert result.finished_halted is True


def test_terminal_ioc_no_fill_requires_a_new_signed_permit(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        open_fill_attempts=(False, True),
    )
    evidence_path = tmp_path / "ioc-no-fill-retry.json"
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=evidence_path,
    )

    result = live_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert result.passed is False
    assert result.retryable is True
    assert result.error_code == "DEGRADED_NO_FILL"
    assert "new signed permit" in result.failure_reason
    assert adapter.calls.count("open") == 1
    assert adapter.calls.count("observe") == 1
    assert adapter.requests["open"][0]["intent_id"] == (
        authorization.intent_id
    )
    assert adapter.requests["open"][0]["side_effect_id"] == (
        executor.deterministic_side_effect_id(
            authorization,
            "OPEN",
        )
    )
    assert result.close_submitted is False
    assert result.finished_halted is True
    evidence = json.loads(evidence_path.read_text(encoding="ascii"))
    assert evidence["round_trip_count"] == 0
    assert evidence["retryable"] is True


def test_terminal_ioc_no_fill_never_proves_round_trip_completion(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        open_fill_attempts=(False, False),
    )
    evidence_path = tmp_path / "ioc-no-fill-exhausted.json"
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=evidence_path,
    )

    result = live_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert result.passed is False
    assert result.retryable is True
    assert result.error_code == "DEGRADED_NO_FILL"
    assert adapter.calls.count("open") == 1
    assert adapter.calls.count("observe") == 1
    assert adapter.calls.count("close") == 0
    assert result.close_submitted is False
    assert result.finished_halted is True
    evidence = json.loads(evidence_path.read_text(encoding="ascii"))
    assert evidence["round_trip_count"] == 0
    assert evidence["target_symbol_flat"] is True


def test_terminal_ioc_no_fill_requires_definitive_exchange_absence(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        observation_status="EXPIRED",
        exchange_evidence_state="unknown",
        open_fill_attempts=(False,),
    )
    evidence_path = tmp_path / "ioc-no-fill-ambiguous.json"
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=evidence_path,
    )

    result = live_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert result.passed is False
    assert result.retryable is False
    assert result.error_code == "OPEN_RESULT_AMBIGUOUS"
    assert "definitive exchange absence proof" in result.failure_reason
    assert result.finished_halted is True


def test_live_preflight_blocks_existing_position_without_closing_it(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        preflight_position_quantity="0.1",
        preflight_position_side="LONG",
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "preflight-position.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert result.passed is False
    assert "preflight target position is not flat" in (
        result.failure_reason
    )
    assert adapter.calls.count("preflight") == 1
    assert "resume" not in adapter.calls
    assert "open" not in adapter.calls
    assert "close" not in adapter.calls
    assert result.finished_halted is True


@pytest.mark.parametrize(
    "failure_message",
    [
        "ownership conflict: projection unavailable",
        "fencing lease lost during readiness timeout",
        "identity conflict: HTTP 503",
        "writer identity conflict: HTTP 503",
        "writer mismatch: HTTP 500",
        "lease conflict: HTTP 503",
        "lease mismatch: HTTP 500",
        "permit journal fsync failed: HTTP 503",
        "durable write failed with ENOSPC: service unavailable",
        "durable journal capacity exhausted",
    ],
)
def test_live_preflight_hard_failure_precedes_soft_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_message: str,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)

    def fail_preflight(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        adapter._record_call("preflight", request)
        raise RuntimeError(failure_message)

    monkeypatch.setattr(adapter, "preflight", fail_preflight)
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "preflight-hard-failure.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert result.passed is False
    assert failure_message in result.failure_reason
    assert adapter.calls.count("preflight") == 1
    assert "resume" not in adapter.calls
    assert "open" not in adapter.calls
    assert "PREFLIGHT" not in result.retry_counts


@pytest.mark.parametrize(
    "failure_message",
    [
        "HTTP 503 ownership telemetry unavailable",
        "HTTP 503 journal projection unavailable",
        "HTTP 503 evidence store unavailable",
        "HTTP 503 capacity exhausted",
    ],
)
def test_structured_preflight_5xx_with_nonconflict_keywords_is_soft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_message: str,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)
    original_preflight = adapter.preflight
    failures = 0

    def soft_preflight(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        nonlocal failures
        if failures < 3:
            failures += 1
            adapter._record_call("preflight", request)
            raise executor.SoftActionFailure(
                failure_message,
                code="HTTP_5XX",
            )
        return original_preflight(request)

    monkeypatch.setattr(adapter, "preflight", soft_preflight)
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "preflight-structured-soft.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert result.passed is True
    assert result.retry_counts["PREFLIGHT"] == 3
    assert adapter.calls.count("open") == 1


def test_before_resume_preflight_transport_failure_degrades_and_continues(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        preflight_failures=3,
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "preflight-degraded.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert result.passed is True
    assert adapter.calls.count("preflight") == 4
    assert adapter.calls.count("open") == 1
    assert any(
        "PREFLIGHT soft failure budget exhausted" in reason
        for reason in result.degraded_reasons
    )


def test_before_open_preflight_transport_failure_blocks_after_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)
    original_preflight = adapter.preflight

    def fail_before_open(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if request.get("phase") == "before-open":
            adapter._record_call("preflight", request)
            raise TimeoutError("exchange authority timeout")
        return original_preflight(request)

    monkeypatch.setattr(adapter, "preflight", fail_before_open)
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "preflight-before-open-timeout.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert result.passed is False
    assert result.error_code == "EXCHANGE_AUTHORITY_UNAVAILABLE"
    assert (
        "before-open exchange authority unavailable after RESUME"
        in result.failure_reason
    )
    assert adapter.calls.count("preflight") == 4
    assert adapter.calls.count("resume") == 1
    assert "open" not in adapter.calls


def test_before_open_preflight_blocks_without_any_exchange_snapshot(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        preflight_failures=6,
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "preflight-no-exchange-snapshot.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert result.passed is False
    assert result.error_code == "EXCHANGE_AUTHORITY_UNAVAILABLE"
    assert "before-open exchange authority unavailable" in (
        result.failure_reason
    )
    assert "open" not in adapter.calls


@pytest.mark.parametrize("authority_value", [None, False])
def test_before_open_preflight_exchange_authority_flag_is_advisory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authority_value: bool | None,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)
    original_preflight = adapter.preflight

    def non_authoritative_preflight(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        payload = dict(original_preflight(request))
        if request.get("phase") != "before-open":
            return payload
        if authority_value is None:
            payload.pop("exchange_authoritative", None)
            return payload
        payload["exchange_authoritative"] = authority_value
        return payload

    monkeypatch.setattr(
        adapter,
        "preflight",
        non_authoritative_preflight,
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "preflight-exchange-authority.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert result.passed is True
    assert any(
        "PREFLIGHT_EXCHANGE_AUTHORITY_UNCONFIRMED" in warning
        for warning in result.warnings
    )
    assert adapter.calls.count("preflight") == 2
    assert adapter.calls.count("open") == 1


@pytest.mark.parametrize(
    "field_name",
    [
        "process_liveness",
        "loss_monitor_healthy",
    ],
)
def test_before_open_preflight_missing_live_health_is_advisory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)
    original_preflight = adapter.preflight

    def unhealthy_preflight(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        payload = dict(original_preflight(request))
        if request.get("phase") != "before-open":
            return payload
        node_snapshot = dict(payload["node_snapshot"])
        node_snapshot.pop(field_name, None)
        payload["node_snapshot"] = node_snapshot
        return payload

    monkeypatch.setattr(adapter, "preflight", unhealthy_preflight)
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / f"preflight-{field_name}.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert result.passed is True
    assert any(
        f"PREFLIGHT_NODE_HEALTH_MISSING: {field_name}" in warning
        for warning in result.warnings
    )
    assert adapter.calls.count("open") == 1


def test_before_open_preflight_missing_trading_state_is_advisory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)
    original_preflight = adapter.preflight

    def missing_state_preflight(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        payload = dict(original_preflight(request))
        if request.get("phase") != "before-open":
            return payload
        node_snapshot = dict(payload["node_snapshot"])
        node_snapshot.pop("trading_state")
        payload["node_snapshot"] = node_snapshot
        return payload

    monkeypatch.setattr(
        adapter,
        "preflight",
        missing_state_preflight,
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "preflight-missing-state.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert result.passed is True
    assert "PREFLIGHT_NODE_STATE_MISSING: trading_state" in (
        result.warnings
    )
    assert adapter.calls.count("open") == 1


@pytest.mark.parametrize(
    "field_name",
    [
        "process_liveness",
        "loss_monitor_healthy",
    ],
)
def test_before_open_preflight_explicit_unhealthy_state_blocks_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)
    original_preflight = adapter.preflight

    def unhealthy_preflight(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        payload = dict(original_preflight(request))
        if request.get("phase") != "before-open":
            return payload
        node_snapshot = dict(payload["node_snapshot"])
        node_snapshot[field_name] = False
        payload["node_snapshot"] = node_snapshot
        return payload

    monkeypatch.setattr(adapter, "preflight", unhealthy_preflight)
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / f"preflight-false-{field_name}.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert result.passed is False
    assert result.failure_reason == (
        f"preflight requires {field_name}=true"
    )
    assert result.finished_halted is True
    assert adapter.calls.count("open") == 0
    assert adapter.calls.count("close") == 0


@pytest.mark.parametrize(
    "raw_warnings",
    [
        None,
        "projection delayed",
        {"detail": "telemetry delayed"},
        [""],
    ],
)
def test_before_open_preflight_malformed_warnings_degrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_warnings: Any,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)
    original_preflight = adapter.preflight

    def malformed_warnings_preflight(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        payload = dict(original_preflight(request))
        if request.get("phase") == "before-open":
            payload["warnings"] = raw_warnings
        return payload

    monkeypatch.setattr(
        adapter,
        "preflight",
        malformed_warnings_preflight,
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "preflight-malformed-warnings.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert result.passed is True
    assert any(
        "PREFLIGHT_WARNINGS_MALFORMED" in warning
        for warning in result.warnings
    )
    assert adapter.calls.count("open") == 1


def test_observation_explicit_unhealthy_loss_monitor_flattens_and_halts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        final_open_filled_quantity="0.03",
    )
    original_observe = adapter.observe

    def unhealthy_observation(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        payload = dict(original_observe(request))
        adapter.position_quantity = Decimal("0.03")
        adapter.position_side = "LONG"
        payload["filled_quantity"] = "0.03"
        payload["loss_monitor_healthy"] = False
        return payload

    monkeypatch.setattr(adapter, "observe", unhealthy_observation)
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "observation-unhealthy.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert result.passed is False
    assert result.failure_reason == (
        "loss_monitor_healthy=false requires risk flattening"
    )
    assert result.finished_halted is True
    assert result.close_submitted is True
    assert result.close_quantity == Decimal("0.03")
    assert adapter.calls.count("open") == 1
    assert adapter.calls.count("close") == 1
    assert adapter.calls.count("halt") == 2
    assert adapter.calls.index("halt") < adapter.calls.index("close")
    assert adapter.close_effect_quantities == [Decimal("0.03")]
    assert adapter.close_requests[0]["quantity"] == "0.03"


@pytest.mark.parametrize(
    ("field_name", "field_value", "expected_error"),
    [
        ("actor_tick_at", None, "preflight actor_tick_at is required"),
        (
            "actor_tick_at",
            NOW - timedelta(seconds=6),
            "preflight actor_tick_at is stale",
        ),
        (
            "loss_monitor_at",
            None,
            "preflight loss_monitor_at is required",
        ),
        (
            "loss_monitor_at",
            NOW - timedelta(seconds=6),
            "preflight loss_monitor_at is stale",
        ),
    ],
)
def test_before_open_preflight_live_progress_is_advisory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    field_value: datetime | None,
    expected_error: str,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)
    original_preflight = adapter.preflight

    def stale_preflight(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        payload = dict(original_preflight(request))
        if request.get("phase") != "before-open":
            return payload
        node_snapshot = dict(payload["node_snapshot"])
        if field_value is None:
            node_snapshot.pop(field_name, None)
        else:
            node_snapshot[field_name] = field_value.isoformat()
        payload["node_snapshot"] = node_snapshot
        return payload

    monkeypatch.setattr(adapter, "preflight", stale_preflight)
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / f"preflight-{field_name}.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert result.passed is True
    assert any(
        expected_error in warning
        for warning in result.warnings
    )
    assert adapter.calls.count("open") == 1


@pytest.mark.parametrize(
    "field_name",
    [
        "writer_id",
        "lease_id",
        "fencing_epoch",
    ],
)
def test_before_open_preflight_missing_extended_identity_is_advisory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)
    original_preflight = adapter.preflight

    def incomplete_preflight(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        payload = dict(original_preflight(request))
        if request.get("phase") != "before-open":
            return payload
        node_snapshot = dict(payload["node_snapshot"])
        node_snapshot.pop(field_name, None)
        payload["node_snapshot"] = node_snapshot
        return payload

    monkeypatch.setattr(adapter, "preflight", incomplete_preflight)
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / f"preflight-missing-{field_name}.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert result.passed is True
    assert any(
        f"PREFLIGHT_NODE_IDENTITY_MISSING: {field_name}" in warning
        for warning in result.warnings
    )
    assert adapter.calls.count("open") == 1


@pytest.mark.parametrize(
    "warning",
    [
        "node telemetry degraded: HTTP_503 node projection unavailable",
        "node telemetry degraded: HTTP_TIMEOUT node telemetry timeout",
        "node telemetry degraded: ADAPTER_ERROR node is missing",
    ],
)
def test_before_open_preflight_allows_unavailable_node_telemetry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    warning: str,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)
    original_preflight = adapter.preflight

    def exchange_only_preflight(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        payload = dict(original_preflight(request))
        if request.get("phase") != "before-open":
            return payload
        payload.pop("node_snapshot")
        payload["warnings"] = [warning]
        return payload

    monkeypatch.setattr(adapter, "preflight", exchange_only_preflight)
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "preflight-node-unavailable.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert result.passed is True
    assert warning in result.warnings
    assert adapter.calls.count("open") == 1


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("node_id", "nautilus-node-account-b"),
        ("writer_id", "writer-account-b"),
        ("lease_id", "lease-account-b"),
        ("fencing_epoch", 43),
    ],
)
def test_before_open_preflight_binds_signed_execution_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    field_value: str | int,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)
    original_preflight = adapter.preflight

    def mismatched_preflight(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        payload = dict(original_preflight(request))
        if request.get("phase") != "before-open":
            return payload
        node_snapshot = dict(payload["node_snapshot"])
        node_snapshot[field_name] = field_value
        payload["node_snapshot"] = node_snapshot
        return payload

    monkeypatch.setattr(adapter, "preflight", mismatched_preflight)
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / f"preflight-{field_name}.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert f"preflight node identity mismatch: {field_name}" in (
        result.failure_reason
    )
    assert "open" not in adapter.calls


def test_before_resume_preflight_blocks_explicit_writer_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)
    original_preflight = adapter.preflight

    def mismatched_preflight(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        payload = dict(original_preflight(request))
        node_snapshot = dict(payload["node_snapshot"])
        node_snapshot["writer_id"] = "writer-account-b"
        payload["node_snapshot"] = node_snapshot
        return payload

    monkeypatch.setattr(adapter, "preflight", mismatched_preflight)
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "preflight-before-resume-writer.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert "preflight node identity mismatch: writer_id" in (
        result.failure_reason
    )
    assert adapter.calls[0] == "preflight"
    assert "resume" not in adapter.calls
    assert "open" not in adapter.calls


def test_live_preflight_rechecks_funds_immediately_before_open(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)

    def expose_position_after_resume() -> None:
        adapter.preflight_position_quantity = "0.1"
        adapter.preflight_position_side = "LONG"

    adapter.on_resume = expose_position_after_resume
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "preflight-before-open.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert "preflight target position is not flat" in (
        result.failure_reason
    )
    assert adapter.calls.count("preflight") == 2
    assert adapter.calls.count("resume") == 1
    assert "open" not in adapter.calls


def test_before_open_preflight_allows_fresh_snapshot_from_before_resume(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        preflight_fetched_at=NOW - timedelta(seconds=1),
        refresh_preflight_after_resume=False,
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "preflight-before-resume-snapshot.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert result.passed is True
    assert any(
        "BEFORE_OPEN_CAUSAL_FRESHNESS_RELAXED" in warning
        for warning in result.warnings
    )
    assert adapter.calls.count("preflight") == 2
    assert adapter.calls.count("resume") == 1
    assert adapter.calls.count("open") == 1
    before_open_request = adapter.requests["preflight"][1]
    assert "exchange_not_before" not in before_open_request
    evidence = json.loads(
        (
            tmp_path / "preflight-before-resume-snapshot.json"
        ).read_text(encoding="ascii")
    )
    relaxed_events = [
        event
        for event in evidence["events"]
        if event["event_type"]
        == "before_open_exchange_causality_relaxed"
    ]
    assert len(relaxed_events) == 1
    assert (
        relaxed_events[0]["payload"]["resume_boundary"]
        == NOW.isoformat()
    )
    assert relaxed_events[0]["payload"]["retained_hard_checks"] == [
        "exchange_max_age",
        "target_position_flat",
        "target_regular_orders_zero",
        "target_algo_orders_zero",
        "known_available_balance_sufficiency",
    ]


def test_non_target_portfolio_drift_is_advisory_for_round_trip(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    drifted_baseline = _digest("unrelated-account-activity")
    adapter = FakeAdapter(
        authorization,
        preflight_baseline_sha256=drifted_baseline,
        final_baseline_sha256=drifted_baseline,
    )
    evidence_path = tmp_path / "non-target-portfolio-drift.json"
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=evidence_path,
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert result.passed is True
    assert result.close_submitted is True
    assert result.finished_halted is True
    assert adapter.calls.count("open") == 1
    assert adapter.calls.count("close") == 1
    assert adapter.calls.count("halt") == 2
    assert any(
        "NON_TARGET_PORTFOLIO_DRIFT" in warning
        for warning in result.warnings
    )
    evidence = json.loads(evidence_path.read_text(encoding="ascii"))
    assert evidence["non_target_portfolio_before_sha256"] == (
        authorization.portfolio_baseline_sha256
    )
    assert evidence["non_target_portfolio_after_sha256"] == (
        drifted_baseline
    )
    assert evidence["target_symbol_flat"] is True
    assert evidence["target_symbol_regular_orders_zero"] is True
    assert evidence["target_symbol_algo_orders_zero"] is True


def test_preflight_only_portfolio_drift_records_phase_bound_hashes(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    drifted_baseline = _digest("transient-unrelated-account-activity")
    adapter = FakeAdapter(
        authorization,
        preflight_baseline_sha256=drifted_baseline,
    )
    evidence_path = tmp_path / "preflight-only-portfolio-drift.json"
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=evidence_path,
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert result.passed is True
    evidence = json.loads(evidence_path.read_text(encoding="ascii"))
    assert evidence["non_target_portfolio_before_sha256"] == (
        authorization.portfolio_baseline_sha256
    )
    assert evidence["non_target_portfolio_after_sha256"] == (
        authorization.portfolio_baseline_sha256
    )
    drift_events = [
        event
        for event in evidence["events"]
        if event["event_type"] == "non_target_portfolio_drift_observed"
    ]
    assert [event["payload"]["phase"] for event in drift_events] == [
        "before-resume",
        "before-open",
    ]
    for event in drift_events:
        assert event["payload"]["signed_baseline_sha256"] == (
            authorization.portfolio_baseline_sha256
        )
        assert event["payload"]["observed_baseline_sha256"] == (
            drifted_baseline
        )


def test_financial_enrichment_failure_commits_degraded_evidence(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        final_enrichment_degraded=True,
        final_warnings=("operator projection unavailable",),
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "financial-degraded.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert result.passed is True
    assert result.failure_reason == ""
    assert result.close_submitted is True
    assert result.finished_halted is True
    assert any(
        "FINANCIAL_PROOF_DEGRADED" in reason
        for reason in result.degraded_reasons
    )


@pytest.mark.parametrize(
    "field_name",
    [
        "enrichment_degraded",
        "financial_proof_complete",
    ],
)
def test_final_snapshot_missing_financial_proof_state_degrades(
    tmp_path: Path,
    field_name: str,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)
    payload = dict(adapter.final_snapshot({}))
    payload.pop(field_name)

    normalized = executor._validate_final_snapshot(
        payload,
        authorization,
        now=NOW,
    )

    assert normalized["financial_proof_complete"] is False
    assert normalized["enrichment_degraded"] is True
    assert any(
        "FINANCIAL_METADATA_INVALID" in warning
        for warning in normalized["warnings"]
    )


def test_final_snapshot_warning_and_proof_state_mismatch_degrades(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)
    payload = dict(adapter.final_snapshot({}))
    payload["warnings"] = ["operator projection unavailable"]

    normalized = executor._validate_final_snapshot(
        payload,
        authorization,
        now=NOW,
    )

    assert normalized["financial_proof_complete"] is False
    assert normalized["enrichment_degraded"] is True
    assert any(
        "FINANCIAL_METADATA_INCONSISTENT" in warning
        for warning in normalized["warnings"]
    )


def test_malformed_final_financial_enrichment_commits_degraded_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)
    original_final_snapshot = adapter.final_snapshot

    def malformed_financial_snapshot(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        payload = dict(original_final_snapshot(request))
        payload["gross_pnl_usdt"] = {"invalid": True}
        payload["fees_usdt"] = "not-a-decimal"
        payload["net_pnl_usdt"] = None
        payload["cumulative_net_loss_usdt"] = ""
        payload["enrichment_degraded"] = "yes"
        payload.pop("financial_proof_complete", None)
        payload["warnings"] = {"detail": "projection unavailable"}
        return payload

    monkeypatch.setattr(
        adapter,
        "final_snapshot",
        malformed_financial_snapshot,
    )
    evidence_path = tmp_path / "malformed-financial-evidence.json"
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=evidence_path,
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert result.passed is True
    assert result.close_submitted is True
    assert result.finished_halted is True
    evidence = json.loads(evidence_path.read_text(encoding="ascii"))
    assert evidence["financial_enrichment_retryable"] is True
    assert evidence["target_symbol_flat"] is True
    assert evidence["target_symbol_regular_orders_zero"] is True
    assert evidence["target_symbol_algo_orders_zero"] is True


def test_live_preflight_blocks_known_insufficient_balance(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        preflight_available_usdt_balance="7.09",
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "preflight-insufficient-balance.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert "available USDT balance is below order plus fee reserve" in (
        result.failure_reason
    )
    assert "open" not in adapter.calls


def test_live_preflight_request_binds_order_and_fee_reserve(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "preflight-order-envelope.json",
    )

    result = live_executor.execute(authorization)

    assert result.passed is True
    requests = adapter.requests["preflight"]
    assert requests[0]["quantity"] == "0.07"
    assert requests[0]["limit_price_usdt"] == "100"
    assert requests[0]["fee_reserve_usdt"] == "0.10"


def test_live_preflight_missing_balance_is_advisory(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        preflight_available_usdt_balance=False,
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "preflight-balance-missing.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert result.passed is True
    assert any(
        "PREFLIGHT_AVAILABLE_BALANCE_MISSING" in warning
        for warning in result.warnings
    )
    assert adapter.calls.count("open") == 1


def test_close_enrichment_degradation_is_retained_in_execution_audit(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    warning = (
        "close operator projection degraded: "
        "filled quantity differs from exchange history"
    )
    adapter = FakeAdapter(
        authorization,
        close_enrichment_degraded=True,
        close_warnings=(warning,),
    )
    evidence_path = tmp_path / "close-enrichment-degraded.json"
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=evidence_path,
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert result.passed is True
    assert any(
        f"CLOSE_ENRICHMENT_DEGRADED: {warning}" == reason
        for reason in result.degraded_reasons
    )
    evidence = json.loads(evidence_path.read_text(encoding="ascii"))
    close_event = next(
        event
        for event in evidence["events"]
        if event["event_type"] == "reduce_only_close_confirmed"
    )
    assert close_event["payload"]["enrichment_warnings"] == [warning]


def test_malformed_close_enrichment_metadata_degrades(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)
    original_submit_close = adapter.submit_close

    def malformed_close_ack(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        payload = dict(original_submit_close(request))
        payload["enrichment_degraded"] = "yes"
        payload["warnings"] = {"detail": "projection unavailable"}
        return payload

    monkeypatch.setattr(
        adapter,
        "submit_close",
        malformed_close_ack,
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "malformed-close-enrichment.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert result.passed is True
    assert result.close_submitted is True
    assert result.finished_halted is True
    assert any(
        "CLOSE_WARNINGS_MALFORMED" in reason
        or "CLOSE_ENRICHMENT_METADATA_INVALID" in reason
        for reason in result.degraded_reasons
    )


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


def test_resume_timeout_exhaustion_blocks_open_while_node_remains_halted(
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

    assert result.status == "BLOCKED"
    assert result.passed is False
    assert "preflight node state is invalid for before-open" in (
        result.failure_reason
    )
    assert adapter.calls.count("resume") == 3
    assert "open" not in adapter.calls
    snapshot = store.snapshot(authorization, mode="live")
    assert snapshot is not None
    assert snapshot["state"] == "HALTED"


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
    assert adapter.close_effect_quantities == [Decimal("0.07")]
    assert result.close_submitted is True
    assert result.finished_halted is True


def test_ambiguous_close_ack_cannot_expand_cumulative_close_quantity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        close_error_after_effect=TimeoutError(
            "injected close response timeout"
        ),
    )
    original_current_position = adapter.current_position
    injected = False

    def current_position(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        nonlocal injected
        if not injected:
            adapter.position_quantity = Decimal("0.10")
            adapter.position_side = "LONG"
            injected = True
        return original_current_position(request)

    monkeypatch.setattr(
        adapter,
        "current_position",
        current_position,
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "close-ack-cumulative-cap.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert adapter.close_effect_quantities == [Decimal("0.07")]
    assert [request["quantity"] for request in adapter.close_requests] == [
        "0.07",
        "0.07",
    ]
    assert (
        adapter.close_requests[0]["side_effect_id"]
        == adapter.close_requests[1]["side_effect_id"]
    )
    assert adapter.position_quantity == Decimal("0.03")
    assert "target position remains after the authorized close" in (
        result.failure_reason
    )
    assert result.finished_halted is True


def test_flat_position_after_close_timeout_stops_close_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)

    def ambiguous_close(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        adapter._require_operation_lock()
        adapter._record_call("close", request)
        adapter.position_quantity = Decimal(0)
        adapter.position_side = "FLAT"
        raise TimeoutError("injected close response timeout")

    monkeypatch.setattr(adapter, "submit_close", ambiguous_close)
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "close-flat-unproven.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert result.passed is True
    assert adapter.calls.count("close") == 1
    assert adapter.calls.count("final-snapshot") == 2
    assert result.close_submitted is True
    assert result.close_quantity == Decimal("0.07")
    assert result.finished_halted is True


def test_pre_open_flat_exchange_sample_cannot_bypass_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)

    def pre_open_flat_position(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        adapter._require_operation_lock()
        adapter._record_call("position", request)
        payload = adapter._identity()
        payload.update(
            {
                "position_side": "FLAT",
                "position_quantity": "0",
                "source": executor.EXCHANGE_EVIDENCE_SOURCE,
                "fetched_at": (
                    NOW - timedelta(seconds=1)
                ).isoformat(),
                "evidence_sha256": _digest("pre-open-flat"),
            }
        )
        return payload

    monkeypatch.setattr(
        adapter,
        "current_position",
        pre_open_flat_position,
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "pre-open-flat.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert result.passed is False
    assert "predates required exchange progress" in result.failure_reason
    assert adapter.calls.count("close") == 0
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
    halt_indexes = [
        index
        for index, action in enumerate(adapter.calls)
        if action == "halt"
    ]
    assert len(final_indexes) == 2
    assert len(halt_indexes) == 2
    assert halt_indexes[0] < final_indexes[0]
    assert final_indexes[0] < halt_indexes[1] < final_indexes[1]
    final_requests = adapter.requests["final-snapshot"]
    assert final_requests[0]["phase"] == "pre-halt-close-proof"
    assert final_requests[1]["phase"] == "post-halt-final"
    assert final_requests[0]["open_side"] == authorization.open_side
    assert final_requests[1]["open_side"] == authorization.open_side
    evidence = json.loads(evidence_path.read_text(encoding="ascii"))
    round_trip = evidence["mainnet_round_trip"]
    assert round_trip["final_snapshot_phase"] == "post-halt-final"
    assert round_trip["final_snapshot_fetched_at"] == NOW.isoformat()
    assert round_trip["final_snapshot_evidence_sha256"] == _digest(
        "post-halt-final"
    )


def test_state_identity_missing_warnings_are_advisory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)
    original_resume = adapter.resume
    original_halt = adapter.halt

    def resume_with_warning(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        payload = dict(original_resume(request))
        payload["warnings"] = [
            "node identity telemetry missing: writer_id"
        ]
        return payload

    def halt_with_warning(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        payload = dict(original_halt(request))
        payload["warnings"] = [
            "node identity telemetry missing: lease_id"
        ]
        return payload

    monkeypatch.setattr(adapter, "resume", resume_with_warning)
    monkeypatch.setattr(adapter, "halt", halt_with_warning)
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "state-identity-warning.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert result.passed is True
    assert result.warnings == (
        "node identity telemetry missing: writer_id",
        "node identity telemetry missing: lease_id",
    )
    assert adapter.calls.count("open") == 1


def test_post_halt_loss_at_permit_cap_blocks_final_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        observation_loss="1.48",
    )
    original_final_snapshot = adapter.final_snapshot

    def final_snapshot(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        payload = dict(original_final_snapshot(request))
        if request.get("phase") == "post-halt-final":
            payload.update(
                {
                    "gross_pnl_usdt": "-1.47",
                    "fees_usdt": "0.02",
                    "net_pnl_usdt": "-1.49",
                    "cumulative_net_loss_usdt": "1.49",
                }
            )
        return payload

    monkeypatch.setattr(adapter, "final_snapshot", final_snapshot)
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "post-halt-loss-cap.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert result.passed is False
    assert result.finished_halted is True
    assert "post-HALT cumulative net loss threshold reached" in (
        result.failure_reason
    )
    assert adapter.calls[-2:] == ["halt", "final-snapshot"]


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
    assert "predates required exchange progress" in result.failure_reason
    assert adapter.calls[-1] == "final-snapshot"


def test_post_halt_residual_position_keeps_permit_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)
    store = _permit_store(tmp_path / "post-halt-residual-ledger.json")
    original_final_snapshot = adapter.final_snapshot

    def final_snapshot(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if request.get("phase") == "post-halt-final":
            adapter.position_quantity = Decimal("0.01")
            adapter.position_side = "LONG"
        return original_final_snapshot(request)

    monkeypatch.setattr(adapter, "final_snapshot", final_snapshot)
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "post-halt-residual.json",
        permit_store=store,
    )

    result = live_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert result.finished_halted is True
    assert adapter.calls.count("close") == 1
    assert adapter.close_effect_quantities == [Decimal("0.07")]
    assert "final snapshot requires target_symbol_flat=true" in (
        result.failure_reason
    )
    outcome = store.claim_or_recover(
        authorization,
        mode="live",
    )
    assert outcome.recovery_required is True
    assert outcome.terminal is False
    assert outcome.state != "EVIDENCE_COMMITTED"
    first_evidence_sha256 = hashlib.sha256(
        (tmp_path / "post-halt-residual.json").read_bytes()
    ).hexdigest()

    restart_store = _permit_store(
        tmp_path / "post-halt-residual-ledger.json"
    )
    restart_adapter = FakeAdapter(authorization)
    crashing_writer = CrashBeforeReplaceEvidenceWriter(
        tmp_path / "post-halt-residual.json"
    )
    restart_executor = _executor(
        tmp_path,
        authorization,
        restart_adapter,
        evidence_path=tmp_path / "post-halt-residual.json",
        permit_store=restart_store,
        evidence_writer=crashing_writer,
    )

    with pytest.raises(
        InjectedCrash,
        match="before recoverable evidence replacement",
    ):
        restart_executor.execute(authorization)

    prepared = restart_store.claim_or_recover(
        authorization,
        mode="live",
    )
    assert prepared.terminal is False
    assert prepared.recovery_required is True
    assert prepared.pending_evidence is not None
    pending_hash = prepared.pending_evidence["sha256"]
    assert pending_hash != first_evidence_sha256

    final_store = _permit_store(
        tmp_path / "post-halt-residual-ledger.json"
    )
    final_adapter = FakeAdapter(authorization)
    final_executor = _executor(
        tmp_path,
        authorization,
        final_adapter,
        evidence_path=tmp_path / "post-halt-residual.json",
        permit_store=final_store,
        evidence_only=True,
    )

    recovered = final_executor.execute(authorization)

    assert recovered.status == "BLOCKED"
    assert recovered.finished_halted is True
    assert recovered.evidence_sha256 == pending_hash
    finalized = final_store.claim_or_recover(
        authorization,
        mode="live",
    )
    assert finalized.terminal is True
    assert finalized.recovery_required is False
    snapshot = final_store.snapshot(authorization, mode="live")
    assert snapshot is not None
    assert snapshot["state"] == "EVIDENCE_COMMITTED"


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
    assert adapter.calls.index("halt") < adapter.calls.index(
        "final-snapshot"
    )
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
        observation_price="172",
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
        == "12.04"
    )


def test_final_fill_price_enforces_notional_when_observation_is_missing(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        observe_failures=3,
        final_open_average_fill_price_usdt="200",
    )
    evidence_path = tmp_path / "final-fill-notional.json"
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=evidence_path,
    )

    result = live_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert "final actual open notional exceeds permit" in (
        result.failure_reason
    )
    assert result.close_submitted is True
    assert result.finished_halted is True
    evidence = json.loads(evidence_path.read_text(encoding="ascii"))
    assert (
        evidence["mainnet_round_trip"]["actual_open_notional_usdt"]
        == "14.00"
    )


def test_halt_precedes_target_risk_cleanup(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "halt-before-cleanup.json",
    )

    result = live_executor.execute(authorization)

    assert result.passed is True
    halt_index = adapter.calls.index("halt")
    assert halt_index < adapter.calls.index("cancel-open")
    assert halt_index < adapter.calls.index("close")


def test_concurrent_target_position_does_not_expand_close_quantity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(authorization)
    original_current_position = adapter.current_position
    injected = False

    def current_position(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        nonlocal injected
        if not injected:
            adapter.position_quantity = Decimal("0.10")
            adapter.position_side = "LONG"
            injected = True
        return original_current_position(request)

    monkeypatch.setattr(
        adapter,
        "current_position",
        current_position,
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "concurrent-target-position.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert "target position exceeded authorized close quantity" in (
        result.failure_reason
    )
    assert len(adapter.close_requests) == 1
    assert adapter.close_requests[0]["quantity"] == "0.07"
    assert result.close_quantity == Decimal("0.07")
    assert result.finished_halted is True


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
        "preflight",
        "resume",
        "preflight",
        "open",
        "observe",
        "halt",
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
def test_signed_live_progress_staleness_after_resume_is_advisory(
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
        halt_observed_at=fresh_at_open,
        resume_observed_at=fresh_at_open,
        post_halt_final_fetched_at=fresh_at_open,
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / f"stale-{field_name}.json",
        clock=clock,
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert result.passed is True
    assert any(
        f"before OPEN {field_name} is stale" in warning
        for warning in result.warnings
    )
    assert adapter.calls.count("open") == 1


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
        resume_observed_at=fresh_at_open,
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
    ("field_name", "age_seconds"),
    [
        ("observed_at", 6),
        ("mark_at", 121),
    ],
)
def test_stale_trade_observation_closes_and_halts(
    tmp_path: Path,
    field_name: str,
    age_seconds: int,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        observation_times={
            field_name: NOW - timedelta(seconds=age_seconds),
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
    assert adapter.calls.index("halt") < adapter.calls.index("close")


def test_stale_loss_monitor_observation_is_advisory(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    adapter = FakeAdapter(
        authorization,
        observation_times={
            "loss_monitor_at": NOW - timedelta(seconds=6),
        },
    )
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "stale-observation-loss-monitor.json",
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert result.passed is True
    assert any(
        "observation loss_monitor_at is stale" in warning
        for warning in result.warnings
    )
    assert adapter.calls.count("open") == 1


@pytest.mark.parametrize(
    ("adapter_kwargs", "expected_error"),
    [
        (
            {"position_source": "projection"},
            "position source must be exchange",
        ),
            (
                {
                    "position_fetched_at": NOW - timedelta(seconds=121),
                },
                "position fetched_at is stale",
        ),
        (
            {"final_source": "projection"},
            "final snapshot source must be exchange",
        ),
            (
                {
                    "final_fetched_at": NOW - timedelta(seconds=121),
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
    assert adapter.calls.count("halt") == 6
    assert adapter.calls.index("close") < (
        len(adapter.calls) - 1 - adapter.calls[::-1].index("halt")
    )
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
    assert adapter.calls.index("halt") < adapter.calls.index("close")
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
        "halt",
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


def test_halted_recovery_finalizer_consumes_normalized_financial_snapshot(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    store = _permit_store(tmp_path / "halted-finalizer-ledger.json")
    _seed_halted_round_trip_journal(
        store,
        authorization,
        close_confirmed=True,
    )
    adapter = FakeAdapter(authorization)
    evidence_path = tmp_path / "halted-finalizer-evidence.json"
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=evidence_path,
        permit_store=store,
        evidence_only=True,
    )

    result = live_executor.execute(authorization)

    assert result.status == "PASSED"
    assert result.finished_halted is True
    assert result.close_submitted is True
    assert result.close_quantity == Decimal("0.07")
    assert adapter.calls == ["final-snapshot"]
    assert adapter.requests["final-snapshot"][0]["phase"] == (
        "recovery-only-final"
    )
    evidence = json.loads(evidence_path.read_text(encoding="ascii"))
    round_trip = evidence["mainnet_round_trip"]
    assert round_trip["open_filled_quantity"] == "0.07"
    assert round_trip["close_filled_quantity"] == "0.07"
    snapshot = store.snapshot(authorization, mode="live")
    assert snapshot is not None
    assert snapshot["state"] == "EVIDENCE_COMMITTED"
    assert snapshot["pending_action"] == ""
    assert snapshot["evidence_sha256"] == result.evidence_sha256


def test_explicit_evidence_only_recovery_accepts_expired_permit(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    authorization = replace(
        authorization,
        expires_at=NOW - timedelta(minutes=1),
    )
    store = _permit_store(
        tmp_path / "expired-evidence-only-ledger.json"
    )
    _seed_halted_round_trip_journal(
        store,
        authorization,
        close_confirmed=True,
    )
    adapter = FakeAdapter(authorization)
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "expired-evidence-only.json",
        permit_store=store,
        evidence_only=True,
    )

    result = live_executor.execute(authorization)

    assert result.status == "PASSED"
    assert adapter.calls == ["final-snapshot"]
    snapshot = store.snapshot(authorization, mode="live")
    assert snapshot is not None
    assert snapshot["state"] == "EVIDENCE_COMMITTED"


def test_explicit_evidence_only_recovery_rejects_non_halted_journal(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    store = _permit_store(
        tmp_path / "ineligible-evidence-only-ledger.json"
    )
    store.claim(authorization, mode="live")
    adapter = FakeAdapter(authorization)
    evidence_path = tmp_path / "ineligible-evidence-only.json"
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=evidence_path,
        permit_store=store,
        evidence_only=True,
    )

    result = live_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert result.error_code == "RECOVERY_JOURNAL_INELIGIBLE"
    assert adapter.calls == []
    assert evidence_path.exists() is False
    snapshot = store.snapshot(authorization, mode="live")
    assert snapshot is not None
    assert snapshot["state"] == "AUTHORIZED"


def test_normal_live_mode_requires_recovery_gate_for_halted_finalization(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    store_path = tmp_path / "normal-halted-gate-ledger.json"
    store = _permit_store(store_path)
    _seed_halted_round_trip_journal(
        store,
        authorization,
        close_confirmed=True,
    )
    before = store_path.read_bytes()
    adapter = FakeAdapter(authorization)
    evidence_path = tmp_path / "normal-halted-gate-evidence.json"
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=evidence_path,
        permit_store=store,
    )

    result = live_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert result.error_code == "RECOVERY_GATE_REQUIRED"
    assert result.finished_halted is True
    assert adapter.calls == []
    assert evidence_path.exists() is False
    assert store_path.read_bytes() == before


def test_normal_live_mode_requires_recovery_gate_for_prepared_evidence(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    store_path = tmp_path / "normal-prepared-gate-ledger.json"
    store = _permit_store(store_path)
    _seed_halted_round_trip_journal(
        store,
        authorization,
        close_confirmed=True,
    )
    evidence_path = tmp_path / "normal-prepared-gate-evidence.json"
    preparing_executor = _executor(
        tmp_path,
        authorization,
        FakeAdapter(authorization),
        evidence_path=evidence_path,
        permit_store=store,
        evidence_writer=CrashBeforePublishEvidenceWriter(evidence_path),
        evidence_only=True,
    )
    with pytest.raises(InjectedCrash):
        preparing_executor.execute(authorization)
    before = store_path.read_bytes()
    adapter = FakeAdapter(authorization)
    normal_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=evidence_path,
        permit_store=_permit_store(store_path),
    )

    result = normal_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert result.error_code == "RECOVERY_GATE_REQUIRED"
    assert adapter.calls == []
    assert evidence_path.exists() is False
    assert store_path.read_bytes() == before


def test_normal_live_mode_requires_recovery_gate_for_terminal_file_restore(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    store_path = tmp_path / "normal-terminal-gate-ledger.json"
    store = _permit_store(store_path)
    _seed_halted_round_trip_journal(
        store,
        authorization,
        close_confirmed=True,
    )
    evidence_path = tmp_path / "normal-terminal-gate-evidence.json"
    recovery_executor = _executor(
        tmp_path,
        authorization,
        FakeAdapter(authorization),
        evidence_path=evidence_path,
        permit_store=store,
        evidence_only=True,
    )
    recovery_executor.execute(authorization)
    evidence_path.unlink()
    before = store_path.read_bytes()
    adapter = FakeAdapter(authorization)
    normal_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=evidence_path,
        permit_store=_permit_store(store_path),
    )

    result = normal_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert result.error_code == "RECOVERY_GATE_REQUIRED"
    assert adapter.calls == []
    assert evidence_path.exists() is False
    assert store_path.read_bytes() == before


def test_halted_recovery_finalizer_accepts_flat_exchange_after_close_ack_timeout(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    store = _permit_store(tmp_path / "halted-close-timeout-ledger.json")
    _seed_halted_round_trip_journal(
        store,
        authorization,
        close_confirmed=False,
    )
    adapter = FakeAdapter(authorization)
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "halted-close-timeout-evidence.json",
        permit_store=store,
        evidence_only=True,
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert any(
        "CLOSE_ACK_MISSING_RECOVERED_FROM_EXCHANGE_SNAPSHOT" in reason
        for reason in result.degraded_reasons
    )
    assert result.close_quantity == Decimal("0.07")
    assert adapter.calls == ["final-snapshot"]
    snapshot = store.snapshot(authorization, mode="live")
    assert snapshot is not None
    assert snapshot["state"] == "EVIDENCE_COMMITTED"


def test_halted_recovery_finalizer_commits_degraded_financial_evidence(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    store = _permit_store(tmp_path / "halted-missing-audit-ledger.json")
    _seed_halted_round_trip_journal(
        store,
        authorization,
        close_confirmed=False,
    )
    adapter = FakeAdapter(
        authorization,
        final_enrichment_degraded=True,
        final_warnings=("trade history is not available yet",),
        final_open_filled_quantity="0",
        final_open_average_fill_price_usdt="0",
    )
    evidence_path = tmp_path / "halted-missing-audit-evidence.json"
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=evidence_path,
        permit_store=store,
        evidence_only=True,
    )

    result = live_executor.execute(authorization)

    assert result.status == "DEGRADED"
    assert result.passed is True
    assert result.retryable is False
    assert result.failure_reason == ""
    assert any(
        "FINANCIAL_PROOF_DEGRADED" in reason
        for reason in result.degraded_reasons
    )
    assert adapter.calls == ["final-snapshot"]
    evidence = json.loads(evidence_path.read_text(encoding="ascii"))
    assert evidence["retryable"] is False
    assert evidence["financial_enrichment_retryable"] is True
    assert evidence["result_status"] == "DEGRADED"
    snapshot = store.snapshot(authorization, mode="live")
    assert snapshot is not None
    assert snapshot["state"] == "EVIDENCE_COMMITTED"
    assert snapshot["pending_action"] == ""
    assert snapshot["evidence_sha256"] == result.evidence_sha256


def test_committed_degraded_evidence_can_be_enriched_read_only(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    store_path = tmp_path / "committed-enrichment-ledger.json"
    store = _permit_store(store_path)
    _seed_halted_round_trip_journal(
        store,
        authorization,
        close_confirmed=True,
    )
    evidence_path = tmp_path / "committed-enrichment.json"
    first_adapter = FakeAdapter(
        authorization,
        final_enrichment_degraded=True,
        final_warnings=("trade history is not available yet",),
        final_open_filled_quantity="0",
        final_open_average_fill_price_usdt="0",
    )
    first_executor = _executor(
        tmp_path,
        authorization,
        first_adapter,
        evidence_path=evidence_path,
        permit_store=store,
        evidence_only=True,
    )

    first_result = first_executor.execute(authorization)

    assert first_result.status == "DEGRADED"
    first_hash = first_result.evidence_sha256
    second_adapter = FakeAdapter(authorization)
    second_executor = _executor(
        tmp_path,
        authorization,
        second_adapter,
        evidence_path=evidence_path,
        permit_store=_permit_store(store_path),
        evidence_only=True,
    )

    second_result = second_executor.execute(authorization)

    assert second_result.status == "PASSED"
    assert second_result.evidence_sha256 != first_hash
    assert second_adapter.calls == ["final-snapshot"]
    evidence = json.loads(evidence_path.read_text(encoding="ascii"))
    assert evidence["financial_enrichment_retryable"] is False
    assert evidence["result_status"] == "PASSED"
    committed = store.snapshot(authorization, mode="live")
    assert committed is not None
    assert committed["state"] == "EVIDENCE_COMMITTED"
    assert committed["evidence_sha256"] == second_result.evidence_sha256


def test_committed_enrichment_publish_crash_recovers_without_adapter_call(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    store_path = tmp_path / "enrichment-crash-ledger.json"
    store = _permit_store(store_path)
    _seed_halted_round_trip_journal(
        store,
        authorization,
        close_confirmed=True,
    )
    evidence_path = tmp_path / "enrichment-crash.json"
    first_executor = _executor(
        tmp_path,
        authorization,
        FakeAdapter(
            authorization,
            final_enrichment_degraded=True,
            final_warnings=("trade history is not available yet",),
            final_open_filled_quantity="0",
            final_open_average_fill_price_usdt="0",
        ),
        evidence_path=evidence_path,
        permit_store=store,
        evidence_only=True,
    )
    first_result = first_executor.execute(authorization)
    crashing_executor = _executor(
        tmp_path,
        authorization,
        FakeAdapter(authorization),
        evidence_path=evidence_path,
        permit_store=_permit_store(store_path),
        evidence_writer=CrashBeforeReplaceEvidenceWriter(evidence_path),
        evidence_only=True,
    )

    with pytest.raises(
        InjectedCrash,
        match="before recoverable evidence replacement",
    ):
        crashing_executor.execute(authorization)

    prepared = store.snapshot(authorization, mode="live")
    assert prepared is not None
    assert prepared["state"] == "EVIDENCE_ENRICHMENT_PENDING"
    assert prepared["evidence_sha256"] == first_result.evidence_sha256
    pending_hash = prepared["pending_evidence"]["sha256"]
    restart_adapter = FakeAdapter(authorization)
    restart_executor = _executor(
        tmp_path,
        authorization,
        restart_adapter,
        evidence_path=evidence_path,
        permit_store=_permit_store(store_path),
        evidence_only=True,
    )

    result = restart_executor.execute(authorization)

    assert result.status == "PASSED"
    assert result.evidence_sha256 == pending_hash
    assert restart_adapter.calls == []
    committed = store.snapshot(authorization, mode="live")
    assert committed is not None
    assert committed["state"] == "EVIDENCE_COMMITTED"
    assert committed["evidence_sha256"] == pending_hash


def test_halted_recovery_residual_risk_becomes_non_reentrant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _authorization(tmp_path)
    store_path = tmp_path / "halted-residual-risk-ledger.json"
    store = _permit_store(store_path)
    _seed_halted_round_trip_journal(
        store,
        authorization,
        close_confirmed=True,
    )
    first_adapter = FakeAdapter(authorization)
    original_final_snapshot = first_adapter.final_snapshot

    def residual_final_snapshot(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        payload = dict(original_final_snapshot(request))
        payload["target_symbol_flat"] = False
        payload["position_quantity"] = "0.01"
        return payload

    monkeypatch.setattr(
        first_adapter,
        "final_snapshot",
        residual_final_snapshot,
    )
    evidence_path = tmp_path / "halted-residual-risk.json"
    first_executor = _executor(
        tmp_path,
        authorization,
        first_adapter,
        evidence_path=evidence_path,
        permit_store=store,
        evidence_only=True,
    )

    first_result = first_executor.execute(authorization)

    assert first_result.status == "BLOCKED"
    assert first_adapter.calls == ["final-snapshot"]
    risk_record = store.snapshot(authorization, mode="live")
    assert risk_record is not None
    assert risk_record["state"] == "RISK_RECOVERY_REQUIRED"
    assert risk_record["pending_action"] == ""

    second_adapter = FakeAdapter(authorization)
    second_executor = _executor(
        tmp_path,
        authorization,
        second_adapter,
        evidence_path=evidence_path,
        permit_store=_permit_store(store_path),
        evidence_only=True,
    )

    second_result = second_executor.execute(authorization)

    assert second_result.status == "BLOCKED"
    assert second_result.error_code == "RISK_RECOVERY_REQUIRED"
    assert second_adapter.calls == []
    assert evidence_path.exists() is False


@pytest.mark.parametrize(
    ("mutation", "failure_text", "expected_state"),
    [
        (
            "position",
            "recovery final snapshot contains residual target risk",
            "RISK_RECOVERY_REQUIRED",
        ),
        (
            "regular-order",
            "recovery final snapshot contains residual target risk",
            "RISK_RECOVERY_REQUIRED",
        ),
        (
            "algo-order",
            "recovery final snapshot contains residual target risk",
            "RISK_RECOVERY_REQUIRED",
        ),
        ("identity", "adapter identity mismatch: account_id", "HALTED"),
        (
            "loss",
            "recovery cumulative net loss threshold reached",
            "HALTED",
        ),
        (
            "notional",
            "recovery actual open notional exceeds permit",
            "HALTED",
        ),
    ],
)
def test_halted_recovery_finalizer_hard_blocks_live_risk_or_identity_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    failure_text: str,
    expected_state: str,
) -> None:
    authorization = _authorization(tmp_path)
    store = _permit_store(
        tmp_path / f"halted-hard-block-{mutation}-ledger.json"
    )
    _seed_halted_round_trip_journal(
        store,
        authorization,
        close_confirmed=True,
    )
    adapter = FakeAdapter(authorization)
    original_final_snapshot = adapter.final_snapshot

    def final_snapshot(
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        payload = dict(original_final_snapshot(request))
        if mutation == "position":
            payload["target_symbol_flat"] = False
            payload["position_quantity"] = "0.01"
        if mutation == "regular-order":
            payload["target_symbol_regular_orders_zero"] = False
        if mutation == "algo-order":
            payload["target_symbol_algo_orders_zero"] = False
        if mutation == "identity":
            payload["account_id"] = "account-b"
        if mutation == "loss":
            payload["enrichment_degraded"] = True
            payload["financial_proof_complete"] = False
            payload["warnings"] = ["financial projection incomplete"]
            payload["gross_pnl_usdt"] = "-1.48"
            payload["fees_usdt"] = "0.02"
            payload["net_pnl_usdt"] = "-1.50"
            payload["cumulative_net_loss_usdt"] = "1.50"
        if mutation == "notional":
            payload["enrichment_degraded"] = True
            payload["financial_proof_complete"] = False
            payload["warnings"] = ["financial projection incomplete"]
            payload["open_average_fill_price_usdt"] = "200"
        return payload

    monkeypatch.setattr(adapter, "final_snapshot", final_snapshot)
    evidence_path = tmp_path / f"halted-hard-block-{mutation}.json"
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=evidence_path,
        permit_store=store,
        evidence_only=True,
    )

    result = live_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert result.retryable is False
    assert failure_text in result.failure_reason
    assert adapter.calls == ["final-snapshot"]
    assert evidence_path.exists() is False
    snapshot = store.snapshot(authorization, mode="live")
    assert snapshot is not None
    assert snapshot["state"] == expected_state
    assert snapshot["evidence_sha256"] == ""


def test_halted_recovery_finalizer_is_idempotent_after_evidence_commit(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    store_path = tmp_path / "halted-idempotent-ledger.json"
    store = _permit_store(store_path)
    _seed_halted_round_trip_journal(
        store,
        authorization,
        close_confirmed=True,
    )
    first_adapter = FakeAdapter(authorization)
    evidence_path = tmp_path / "halted-idempotent-evidence.json"
    first_executor = _executor(
        tmp_path,
        authorization,
        first_adapter,
        evidence_path=evidence_path,
        permit_store=store,
        evidence_only=True,
    )

    first_result = first_executor.execute(authorization)
    committed = store.snapshot(authorization, mode="live")
    assert committed is not None
    committed_history_length = len(committed["history"])
    evidence_path.unlink()

    restart_store = _permit_store(store_path)
    restart_adapter = FakeAdapter(authorization)
    restart_executor = _executor(
        tmp_path,
        authorization,
        restart_adapter,
        evidence_path=evidence_path,
        permit_store=restart_store,
        evidence_only=True,
    )

    second_result = restart_executor.execute(authorization)

    assert second_result.status == first_result.status
    assert second_result.evidence_sha256 == first_result.evidence_sha256
    assert second_result.close_quantity == Decimal("0.07")
    assert restart_adapter.calls == []
    assert evidence_path.exists() is True
    assert hashlib.sha256(evidence_path.read_bytes()).hexdigest() == (
        first_result.evidence_sha256
    )
    finalized = restart_store.snapshot(authorization, mode="live")
    assert finalized is not None
    assert finalized["state"] == "EVIDENCE_COMMITTED"
    assert len(finalized["history"]) == committed_history_length


def test_committed_evidence_hash_conflict_does_not_rewrite_terminal_file(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    store_path = tmp_path / "committed-conflict-ledger.json"
    store = _permit_store(store_path)
    _seed_halted_round_trip_journal(
        store,
        authorization,
        close_confirmed=True,
    )
    first_adapter = FakeAdapter(authorization)
    evidence_path = tmp_path / "committed-conflict-evidence.json"
    first_executor = _executor(
        tmp_path,
        authorization,
        first_adapter,
        evidence_path=evidence_path,
        permit_store=store,
        evidence_only=True,
    )
    first_result = first_executor.execute(authorization)
    evidence_path.chmod(0o600)
    evidence_path.write_text(
        '{"status":"tampered"}\n',
        encoding="ascii",
    )
    evidence_path.chmod(0o400)
    tampered_hash = hashlib.sha256(
        evidence_path.read_bytes()
    ).hexdigest()

    restart_adapter = FakeAdapter(authorization)
    restart_executor = _executor(
        tmp_path,
        authorization,
        restart_adapter,
        evidence_path=evidence_path,
        permit_store=_permit_store(store_path),
        evidence_only=True,
    )

    result = restart_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert result.error_code == "RECOVERY_EVIDENCE_INCOMPLETE"
    assert restart_adapter.calls == []
    assert hashlib.sha256(evidence_path.read_bytes()).hexdigest() == (
        tampered_hash
    )
    committed = store.snapshot(authorization, mode="live")
    assert committed is not None
    assert committed["state"] == "EVIDENCE_COMMITTED"
    assert committed["evidence_sha256"] == first_result.evidence_sha256


def test_halted_recovery_finalizer_restart_publishes_prepared_evidence_read_only(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    store_path = tmp_path / "halted-prepare-crash-ledger.json"
    store = _permit_store(store_path)
    _seed_halted_round_trip_journal(
        store,
        authorization,
        close_confirmed=True,
    )
    first_adapter = FakeAdapter(authorization)
    evidence_path = tmp_path / "halted-prepare-crash-evidence.json"
    crashing_writer = CrashBeforePublishEvidenceWriter(evidence_path)
    first_executor = _executor(
        tmp_path,
        authorization,
        first_adapter,
        evidence_path=evidence_path,
        permit_store=store,
        evidence_writer=crashing_writer,
        evidence_only=True,
    )

    with pytest.raises(
        InjectedCrash,
        match="crash before evidence publication",
    ):
        first_executor.execute(authorization)

    prepared = store.snapshot(authorization, mode="live")
    assert prepared is not None
    assert prepared["state"] == "HALTED"
    assert prepared["pending_action"] == "PUBLISH_EVIDENCE"
    assert evidence_path.exists() is False

    restart_store = _permit_store(store_path)
    restart_adapter = FakeAdapter(authorization)
    restart_executor = _executor(
        tmp_path,
        authorization,
        restart_adapter,
        evidence_path=evidence_path,
        permit_store=restart_store,
        evidence_only=True,
    )

    result = restart_executor.execute(authorization)

    assert result.status == "PASSED"
    assert restart_adapter.calls == []
    assert evidence_path.exists() is True
    finalized = restart_store.snapshot(authorization, mode="live")
    assert finalized is not None
    assert finalized["state"] == "EVIDENCE_COMMITTED"
    assert finalized["evidence_sha256"] == result.evidence_sha256


def test_prepared_evidence_identity_is_validated_before_publication(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    store_path = tmp_path / "invalid-prepared-identity-ledger.json"
    store = _permit_store(store_path)
    _seed_halted_round_trip_journal(
        store,
        authorization,
        close_confirmed=True,
    )
    evidence_path = tmp_path / "invalid-prepared-identity.json"
    first_executor = _executor(
        tmp_path,
        authorization,
        FakeAdapter(authorization),
        evidence_path=evidence_path,
        permit_store=store,
        evidence_writer=CrashBeforePublishEvidenceWriter(evidence_path),
        evidence_only=True,
    )

    with pytest.raises(InjectedCrash):
        first_executor.execute(authorization)

    ledger_payload = json.loads(store_path.read_text(encoding="ascii"))
    record = next(iter(ledger_payload["records"].values()))
    pending = record["pending_evidence"]
    pending_payload = pending["payload"]
    pending_payload["release_id"] = "forged-release"
    forged_hash = hashlib.sha256(
        executor._canonical_pretty_json_bytes(pending_payload)
    ).hexdigest()
    pending["sha256"] = forged_hash
    record["pending_action_payload"]["sha256"] = forged_hash
    store_path.write_bytes(
        executor._canonical_pretty_json_bytes(ledger_payload)
    )

    restart_adapter = FakeAdapter(authorization)
    restart_executor = _executor(
        tmp_path,
        authorization,
        restart_adapter,
        evidence_path=evidence_path,
        permit_store=_permit_store(store_path),
        evidence_only=True,
    )

    result = restart_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert "evidence identity mismatch: release_id" in (
        result.failure_reason
    )
    assert restart_adapter.calls == []
    assert evidence_path.exists() is False
    unchanged = json.loads(store_path.read_text(encoding="ascii"))
    unchanged_record = next(iter(unchanged["records"].values()))
    assert unchanged_record["state"] == "HALTED"
    assert unchanged_record["pending_evidence"]["sha256"] == forged_hash


def test_claim_outcome_preserves_pending_close_request(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    store = _permit_store(tmp_path / "pending-close-outcome-ledger.json")
    close_request = {
        **_base_request(authorization),
        "intent_id": authorization.close_intent_id,
        "open_intent_id": authorization.intent_id,
        "client_order_id": authorization.close_client_order_id,
        "side": "SELL",
        "position_side": "LONG",
        "order_type": "MARKET",
        "quantity": "0.07",
        "reduce_only": True,
        "reason": "failure-cleanup",
        "attempt": 1,
        "side_effect_id": executor.deterministic_side_effect_id(
            authorization,
            "CLOSE",
        ),
        "hard_timeout_seconds": 20.0,
        "exchange_not_before": NOW.isoformat(),
    }
    store.claim(authorization, mode="live")
    store.prepare_action(
        authorization,
        mode="live",
        action="CLOSE",
        payload=close_request,
        state="CLOSE_PENDING",
    )

    outcome = store.claim_or_recover(authorization, mode="live")

    assert outcome.pending_action == "CLOSE"
    assert outcome.pending_action_payload == close_request


def test_restart_replays_exact_pending_close_request(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    store = _permit_store(tmp_path / "pending-close-replay-ledger.json")
    close_request = {
        **_base_request(authorization),
        "mode": "live",
        "authorization_sha256": authorization.authorization_sha256,
        "document_sha256": dict(authorization.document_sha256),
        "intent_id": authorization.close_intent_id,
        "open_intent_id": authorization.intent_id,
        "client_order_id": authorization.close_client_order_id,
        "side": "SELL",
        "position_side": "LONG",
        "order_type": "MARKET",
        "quantity": "0.07",
        "reduce_only": True,
        "reason": "failure-cleanup",
        "attempt": 1,
        "side_effect_id": executor.deterministic_side_effect_id(
            authorization,
            "CLOSE",
        ),
        "hard_timeout_seconds": 20.0,
        "exchange_not_before": NOW.isoformat(),
    }
    store.claim(authorization, mode="live")
    store.prepare_action(
        authorization,
        mode="live",
        action="CLOSE",
        payload=close_request,
        state="CLOSE_PENDING",
    )
    adapter = FakeAdapter(authorization)
    adapter.position_quantity = Decimal("0.02")
    adapter.position_side = "LONG"
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "pending-close-replay.json",
        permit_store=store,
    )

    result = live_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert "cancel-open" not in adapter.calls
    assert adapter.calls.index("position") < adapter.calls.index("close")
    assert adapter.close_requests == [close_request]
    assert adapter.close_effect_quantities == [Decimal("0.07")]


def test_pending_close_survives_recovery_position_query_failure(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    store = _permit_store(
        tmp_path / "pending-close-query-failure-ledger.json"
    )
    close_request = {
        **_base_request(authorization),
        "mode": "live",
        "authorization_sha256": authorization.authorization_sha256,
        "document_sha256": dict(authorization.document_sha256),
        "intent_id": authorization.close_intent_id,
        "open_intent_id": authorization.intent_id,
        "client_order_id": authorization.close_client_order_id,
        "side": "SELL",
        "position_side": "LONG",
        "order_type": "MARKET",
        "quantity": "0.07",
        "reduce_only": True,
        "reason": "failure-cleanup",
        "attempt": 1,
        "side_effect_id": executor.deterministic_side_effect_id(
            authorization,
            "CLOSE",
        ),
        "hard_timeout_seconds": 20.0,
        "exchange_not_before": NOW.isoformat(),
    }
    store.claim(authorization, mode="live")
    store.prepare_action(
        authorization,
        mode="live",
        action="CLOSE",
        payload=close_request,
        state="CLOSE_PENDING",
    )
    adapter = FakeAdapter(
        authorization,
        position_failures=3,
    )
    adapter.position_quantity = Decimal("0.02")
    adapter.position_side = "LONG"
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=tmp_path / "pending-close-query-failure.json",
        permit_store=store,
        recovery_max_attempts=3,
    )

    result = live_executor.execute(authorization)

    assert result.status == "BLOCKED"
    assert "close" not in adapter.calls
    snapshot = store.snapshot(authorization, mode="live")
    assert snapshot is not None
    assert snapshot["state"] == "CLOSE_PENDING"
    assert snapshot["pending_action"] == "CLOSE"
    assert snapshot["pending_action_payload"] == close_request


def test_recovery_history_accepts_production_close_quantity_field(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    store = _permit_store(tmp_path / "close-history-quantity-ledger.json")
    store.claim(authorization, mode="live")
    close_request = {
        **_base_request(authorization),
        "client_order_id": authorization.close_client_order_id,
        "quantity": "0.07",
        "reduce_only": True,
        "side_effect_id": executor.deterministic_side_effect_id(
            authorization,
            "CLOSE",
        ),
    }
    store.prepare_action(
        authorization,
        mode="live",
        action="CLOSE",
        payload=close_request,
        state="CLOSE_PENDING",
    )
    store.complete_action(
        authorization,
        mode="live",
        action="CLOSE",
        state=None,
        result={
            "client_order_id": authorization.close_client_order_id,
            "quantity": "0.07",
        },
    )
    store.mark_state(
        authorization,
        mode="live",
        state="HALTED",
        event_type="HALTED",
        payload={
            "trading_state": "HALTED",
            "observed_at": NOW.isoformat(),
        },
    )
    record = store.snapshot(authorization, mode="live")
    assert record is not None

    recovery = executor._recovery_history_evidence(
        record,
        authorization,
    )

    assert recovery["requested_close_quantity"] == Decimal("0.07")
    assert recovery["confirmed_close_quantity"] == Decimal("0.07")


def test_recovery_history_rejects_conflicting_close_quantity_fields(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    store = _permit_store(tmp_path / "close-history-conflict-ledger.json")
    store.claim(authorization, mode="live")
    close_request = {
        **_base_request(authorization),
        "client_order_id": authorization.close_client_order_id,
        "quantity": "0.07",
        "reduce_only": True,
        "side_effect_id": executor.deterministic_side_effect_id(
            authorization,
            "CLOSE",
        ),
    }
    store.prepare_action(
        authorization,
        mode="live",
        action="CLOSE",
        payload=close_request,
        state="CLOSE_PENDING",
    )
    store.complete_action(
        authorization,
        mode="live",
        action="CLOSE",
        state="CLOSE_CONFIRMED",
        result={
            "client_order_id": authorization.close_client_order_id,
            "quantity": "0.07",
            "filled_quantity": "0.06",
        },
    )
    store.mark_state(
        authorization,
        mode="live",
        state="HALTED",
        event_type="HALTED",
        payload={
            "trading_state": "HALTED",
            "observed_at": NOW.isoformat(),
        },
    )
    record = store.snapshot(authorization, mode="live")
    assert record is not None

    with pytest.raises(
        executor.LiveTradeExecutionError,
        match="confirmation quantity fields differ",
    ):
        executor._recovery_history_evidence(
            record,
            authorization,
        )


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
    assert adapter.close_requests == [
        adapter.close_requests[0],
        adapter.close_requests[0],
        adapter.close_requests[0],
    ]
    assert result.finished_halted is True
    outcome = store.claim_or_recover(
        authorization,
        mode="live",
    )
    assert outcome.recovery_required is True
    assert outcome.terminal is False
    assert outcome.state != "EVIDENCE_COMMITTED"


def test_final_halt_retries_after_initial_halt_exhaustion(
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
    assert result.finished_halted is True
    assert adapter.calls.count("halt") == 4
    assert adapter.calls.index("close") < (
        len(adapter.calls) - 1 - adapter.calls[::-1].index("halt")
    )
    assert "unable to prove HALTED within recovery budget" in (
        result.failure_reason
    )
    outcome = store.claim_or_recover(
        authorization,
        mode="live",
    )
    assert outcome.recovery_required is False
    assert outcome.terminal is True


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
    first_halt = history_states.index("HALTED")
    close_pending = history_states.index("CLOSE_PENDING")
    close_confirmed = history_states.index("CLOSE_CONFIRMED")
    final_halt = len(history_states) - 1 - history_states[::-1].index(
        "HALTED"
    )
    evidence_committed = history_states.index("EVIDENCE_COMMITTED")
    assert history_states.index("OPEN_OBSERVED") < first_halt
    assert first_halt < close_pending < close_confirmed
    assert close_confirmed < final_halt < evidence_committed


def test_successful_short_round_trip_closes_buy_reduce_only(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path, open_side="SELL")
    adapter = FakeAdapter(authorization)
    evidence_path = tmp_path / "short-success-evidence.json"
    live_executor = _executor(
        tmp_path,
        authorization,
        adapter,
        evidence_path=evidence_path,
    )

    result = live_executor.execute(authorization)

    assert result.passed is True
    assert adapter.requests["open"][0]["side"] == "SELL"
    close_request = adapter.close_requests[0]
    assert close_request["side"] == "BUY"
    assert close_request["position_side"] == "SHORT"
    assert close_request["reduce_only"] is True
    assert close_request["quantity"] == "0.07"
    final_requests = adapter.requests["final-snapshot"]
    assert final_requests[-1]["open_side"] == "SELL"
    evidence = json.loads(evidence_path.read_text(encoding="ascii"))
    round_trip = evidence["mainnet_round_trip"]
    assert round_trip["open_side"] == "SELL"
    assert round_trip["open_filled_quantity"] == "0.07"
    assert round_trip["close_filled_quantity"] == "0.07"
    assert round_trip["target_symbol_flat"] is True
    assert round_trip["target_symbol_regular_orders_zero"] is True
    assert round_trip["target_symbol_algo_orders_zero"] is True
    assert round_trip["cumulative_net_loss_usdt"] == "0.10"
    assert result.finished_halted is True


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
        evidence_only=True,
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
        evidence_only=True,
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


def test_live_evidence_parent_swap_to_symlink_blocks_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_parent = tmp_path / "bound-evidence-parent"
    original_parent.mkdir(mode=0o700)
    attacker_parent = tmp_path / "redirected-evidence-parent"
    attacker_parent.mkdir(mode=0o700)
    target = original_parent / "evidence.json"
    original_payload = b'{"sentinel":"original"}\n'
    target.write_bytes(original_payload)
    target.chmod(0o400)
    writer = executor.AtomicEvidenceWriter(target, live=True)
    prepared, prepared_hash = writer.prepare(
        {
            "schema_version": executor.EVIDENCE_SCHEMA,
            "passed": True,
        }
    )
    moved_parent = tmp_path / "bound-evidence-parent-original"
    original_mkstemp = executor.tempfile.mkstemp
    swapped = False

    def swapping_mkstemp(*args, **kwargs):
        nonlocal swapped
        if swapped is False:
            swapped = True
            original_parent.rename(moved_parent)
            original_parent.symlink_to(
                attacker_parent,
                target_is_directory=True,
            )
        return original_mkstemp(*args, **kwargs)

    monkeypatch.setattr(
        executor.tempfile,
        "mkstemp",
        swapping_mkstemp,
    )

    with pytest.raises(
        executor.LiveTradeExecutionError,
        match="protected evidence parent changed|symlink",
    ):
        writer.commit_prepared(
            prepared,
            evidence_sha256=prepared_hash,
            allow_existing=False,
            replace_existing=True,
        )

    assert swapped is True
    assert (moved_parent / "evidence.json").read_bytes() == (
        original_payload
    )
    assert (attacker_parent / "evidence.json").exists() is False
    assert list(attacker_parent.glob(".evidence.json.*.tmp")) == []


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


def test_live_cli_evidence_only_accepts_expired_stale_runtime_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger_path = (tmp_path / "live-recovery-ledger.json").resolve()
    monkeypatch.setattr(
        executor,
        "DEFAULT_LIVE_PERMIT_LEDGER_PATH",
        ledger_path,
    )

    def stale_runtime_binding(
        documents: dict[str, dict[str, Any]],
    ) -> None:
        for payload in documents.values():
            payload["live_executor_sha256"] = "a" * 64
            payload["live_adapter_sha256"] = "b" * 64

    paths = _write_authorization_files(
        tmp_path,
        mutate=stale_runtime_binding,
        rebuild_hash_chain=True,
        signed=True,
    )
    verifier = FakeSignatureVerifier()
    with _operation_lock(tmp_path) as operation_lock:
        authorization = executor.load_authorization(
            paths,
            execute_live=True,
            signature_verifier=verifier,
            operation_lock=operation_lock,
            now=NOW,
        )
    store = executor.SingleUsePermitStore(ledger_path)
    _seed_halted_round_trip_journal(
        store,
        authorization,
        close_confirmed=True,
    )
    adapter = FakeAdapter(authorization)
    adapter_path = _write_live_adapter(tmp_path)

    class HeldLiveOperationLock(AlwaysHeldOperationLock):
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    class RecoveryJsonAdapter:
        def __init__(self, *_args, **_kwargs) -> None:
            return

        def __enter__(self):
            return adapter

        def __exit__(self, *_args) -> None:
            return None

    original_executor = executor.AccountALiveTradeExecutor

    def fixed_time_executor(**kwargs):
        kwargs["clock"] = lambda: NOW
        kwargs["sleeper"] = lambda _seconds: None
        return original_executor(**kwargs)

    monkeypatch.setattr(
        executor,
        "OpenSSLSignatureVerifier",
        FakeSignatureVerifier,
    )
    monkeypatch.setattr(
        executor,
        "LiveOperationLock",
        HeldLiveOperationLock,
    )
    monkeypatch.setattr(
        executor,
        "JsonCommandAdapter",
        RecoveryJsonAdapter,
    )
    monkeypatch.setattr(
        executor,
        "AccountALiveTradeExecutor",
        fixed_time_executor,
    )
    evidence_path = (tmp_path / "live-recovery-evidence.json").resolve()
    recovery_gate = _write_evidence_recovery_gate(
        tmp_path,
        authorization,
        ledger_path=ledger_path,
        evidence_path=evidence_path,
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
            str(ledger_path),
            "--evidence-output",
            str(evidence_path),
            "--execute-live",
            "--recover-evidence-only",
            "--evidence-recovery-gate",
            str(recovery_gate.payload),
            "--evidence-recovery-gate-signature",
            str(recovery_gate.signature),
            "--live-adapter",
            str(adapter_path),
        ]
    )

    assert exit_code == 0, capsys.readouterr().err
    summary = json.loads(capsys.readouterr().out)
    assert summary["passed"] is True
    assert adapter.calls == ["final-snapshot"]
    committed = store.snapshot(authorization, mode="live")
    assert committed is not None
    assert committed["state"] == "EVIDENCE_COMMITTED"


def test_live_cli_evidence_only_requires_signed_recovery_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger_path = (tmp_path / "missing-recovery-gate-ledger.json").resolve()
    monkeypatch.setattr(
        executor,
        "DEFAULT_LIVE_PERMIT_LEDGER_PATH",
        ledger_path,
    )
    paths = _write_authorization_files(tmp_path, signed=True)
    verifier = FakeSignatureVerifier()
    with _operation_lock(tmp_path) as operation_lock:
        authorization = executor.load_authorization(
            paths,
            execute_live=True,
            signature_verifier=verifier,
            operation_lock=operation_lock,
            now=NOW,
        )
    store = executor.SingleUsePermitStore(ledger_path)
    _seed_halted_round_trip_journal(
        store,
        authorization,
        close_confirmed=True,
    )
    adapter_path = _write_live_adapter(tmp_path)

    class HeldLiveOperationLock(AlwaysHeldOperationLock):
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    monkeypatch.setattr(
        executor,
        "OpenSSLSignatureVerifier",
        FakeSignatureVerifier,
    )
    monkeypatch.setattr(
        executor,
        "LiveOperationLock",
        HeldLiveOperationLock,
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
            "--evidence-output",
            str((tmp_path / "missing-recovery-gate.json").resolve()),
            "--execute-live",
            "--recover-evidence-only",
            "--live-adapter",
            str(adapter_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "requires --evidence-recovery-gate" in captured.err


def test_live_cli_evidence_only_rejects_recovery_runtime_hash_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger_path = (tmp_path / "recovery-runtime-drift-ledger.json").resolve()
    monkeypatch.setattr(
        executor,
        "DEFAULT_LIVE_PERMIT_LEDGER_PATH",
        ledger_path,
    )
    paths = _write_authorization_files(tmp_path, signed=True)
    verifier = FakeSignatureVerifier()
    with _operation_lock(tmp_path) as operation_lock:
        authorization = executor.load_authorization(
            paths,
            execute_live=True,
            signature_verifier=verifier,
            operation_lock=operation_lock,
            now=NOW,
        )
    store = executor.SingleUsePermitStore(ledger_path)
    _seed_halted_round_trip_journal(
        store,
        authorization,
        close_confirmed=True,
    )
    adapter_path = _write_live_adapter(tmp_path)
    evidence_path = (tmp_path / "recovery-runtime-drift.json").resolve()
    recovery_gate = _write_evidence_recovery_gate(
        tmp_path,
        authorization,
        ledger_path=ledger_path,
        evidence_path=evidence_path,
        executor_sha256="f" * 64,
    )

    class HeldLiveOperationLock(AlwaysHeldOperationLock):
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    monkeypatch.setattr(
        executor,
        "OpenSSLSignatureVerifier",
        FakeSignatureVerifier,
    )
    monkeypatch.setattr(
        executor,
        "LiveOperationLock",
        HeldLiveOperationLock,
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
            "--evidence-output",
            str(evidence_path),
            "--execute-live",
            "--recover-evidence-only",
            "--evidence-recovery-gate",
            str(recovery_gate.payload),
            "--evidence-recovery-gate-signature",
            str(recovery_gate.signature),
            "--live-adapter",
            str(adapter_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "live executor hash differs from signed release" in captured.err


def _authorization(
    tmp_path: Path,
    *,
    open_side: str = "BUY",
) -> executor.CanaryAuthorization:
    mutate = None
    rebuild_hash_chain = False
    if open_side != "BUY":
        mutate = lambda documents: documents["permit"].update(
            {"open_side": open_side}
        )
        rebuild_hash_chain = True
    paths = _write_authorization_files(
        tmp_path,
        mutate=mutate,
        rebuild_hash_chain=rebuild_hash_chain,
    )
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


def _write_evidence_recovery_gate(
    root: Path,
    authorization: executor.CanaryAuthorization,
    *,
    ledger_path: Path,
    evidence_path: Path,
    executor_sha256: str = LIVE_EXECUTOR_SHA256,
    adapter_sha256: str = LIVE_ADAPTER_SHA256,
) -> executor.SignedDocumentPaths:
    payload = {
        "schema_version": executor.EVIDENCE_RECOVERY_GATE_SCHEMA,
        "gate_id": str(uuid4()),
        "capability": executor.EVIDENCE_RECOVERY_CAPABILITY,
        "allowed_actions": [
            "final-snapshot",
            "publish-evidence",
        ],
        "account_id": authorization.account_id,
        "symbol": authorization.symbol,
        "permit_id": authorization.permit_id,
        "authorization_sha256": authorization.authorization_sha256,
        "release_id": authorization.release.release_id,
        "intent_id": authorization.intent_id,
        "close_intent_id": authorization.close_intent_id,
        "open_client_order_id": authorization.open_client_order_id,
        "close_client_order_id": authorization.close_client_order_id,
        "permit_store_id": authorization.release.permit_store_id,
        "permit_store_path": str(ledger_path),
        "evidence_path": str(evidence_path),
        "recovery_executor_sha256": executor_sha256,
        "recovery_adapter_sha256": adapter_sha256,
        "issued_at": "2026-08-08T11:00:00+00:00",
        "refresh_after": "2027-08-08T11:00:00+00:00",
    }
    payload_bytes = _json_bytes(payload)
    payload_path = root / f"evidence-recovery-gate-{uuid4()}.json"
    signature_path = payload_path.with_suffix(".sig")
    payload_path.write_bytes(payload_bytes)
    signature_path.write_bytes(
        hashlib.sha256(payload_bytes).hexdigest().encode("ascii")
    )
    return executor.SignedDocumentPaths(
        payload_path,
        signature_path,
    )


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


def _seed_halted_round_trip_journal(
    store: executor.SingleUsePermitStore,
    authorization: executor.CanaryAuthorization,
    *,
    close_confirmed: bool,
) -> None:
    store.claim(authorization, mode="live")
    store.mark_state(
        authorization,
        mode="live",
        state="OPEN_OBSERVED",
        event_type="OPEN_OBSERVED",
        payload={
            "client_order_id": authorization.open_client_order_id,
            "filled_quantity": "0.07",
            "average_fill_price_usdt": "100",
        },
    )
    close_request = {
        **_base_request(authorization),
        "intent_id": authorization.close_intent_id,
        "open_intent_id": authorization.intent_id,
        "client_order_id": authorization.close_client_order_id,
        "quantity": "0.07",
        "reduce_only": True,
        "side_effect_id": executor.deterministic_side_effect_id(
            authorization,
            "CLOSE",
        ),
    }
    store.prepare_action(
        authorization,
        mode="live",
        action="CLOSE",
        payload=close_request,
        state="CLOSE_PENDING",
    )
    if close_confirmed:
        store.complete_action(
            authorization,
            mode="live",
            action="CLOSE",
            state="CLOSE_CONFIRMED",
            result={
                "client_order_id": authorization.close_client_order_id,
                "quantity": "0.07",
            },
        )
    store.mark_state(
        authorization,
        mode="live",
        state="HALTED",
        event_type="HALTED",
        payload={
            "trading_state": "HALTED",
            "observed_at": NOW.isoformat(),
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
    evidence_only: bool = False,
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
        evidence_only=evidence_only,
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
        "node_id": "nautilus-node-account-a",
        "writer_id": "writer-account-a",
        "lease_id": "lease-account-a",
        "fencing_epoch": 42,
        "image_digest": "sha256:" + ("1" * 64),
        "config_sha256": "2" * 64,
        "dependency_lock_sha256": "3" * 64,
        "live_executor_sha256": LIVE_EXECUTOR_SHA256,
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
        "process_liveness": True,
        "loss_monitor_healthy": True,
        "ownership_healthy": True,
        "fencing_healthy": True,
        "risk_healthy": True,
        "durability_healthy": True,
        "no_open_p0_p1_incidents": True,
        "non_target_portfolio_baseline_sha256": "4" * 64,
        "actor_tick_at": NOW.isoformat(),
        "heartbeat_at": NOW.isoformat(),
        "projection_at": NOW.isoformat(),
        "reconciliation_at": NOW.isoformat(),
        "loss_monitor_at": NOW.isoformat(),
        "health_max_age_seconds": "5",
        "exchange_max_age_seconds": "120",
        **window,
    }
    safety_hash = _digest_json(safety_gate)
    emergency_gate = {
        "schema_version": executor.EMERGENCY_CLOSE_GATE_SCHEMA,
        **release_identity,
        "release_gate_sha256": release_hash,
        "safety_gate_sha256": safety_hash,
        "verified": True,
        "evidence_type": "testnet_execution",
        "environment": "testnet",
        "open_order_type": "LIMIT",
        "open_time_in_force": "IOC",
        "close_order_type": "MARKET",
        "close_reduce_only": True,
        "verified_quantity": "0.07",
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
        "quantity": "0.07",
        "limit_price_usdt": "100",
        "max_notional_usdt": "12",
        "fee_reserve_usdt": "0.10",
        "exchange_filters": {
            "quantity_step": "0.01",
            "min_quantity": "0.01",
            "price_tick": "0.01",
            "min_notional": "5",
        },
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
