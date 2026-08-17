from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Callable, Literal, Mapping
from uuid import UUID

_PROCESS_LOCKS_GUARD = Lock()
_PROCESS_LOCKS: dict[str, RLock] = {}
LIVE_CANARY_ACCOUNT_IDS = frozenset(
    {
        "account-a",
        "account-b",
        "account-c",
        "account-d",
    }
)
LIVE_OPEN_MODE_CANARY_ONLY = "canary_only"
LIVE_OPEN_MODE_NORMAL = "normal"
LIVE_OPEN_MODES = frozenset(
    {
        LIVE_OPEN_MODE_CANARY_ONLY,
        LIVE_OPEN_MODE_NORMAL,
    }
)
LIVE_OPEN_NORMAL_PHASE = "fleet_complete"


def is_live_canary_account(account_id: Any) -> bool:
    return str(account_id or "").strip() in LIVE_CANARY_ACCOUNT_IDS


def live_canary_permit_required(
    account_id: Any,
    rollout_phase: Any = None,
) -> bool:
    if not is_live_canary_account(account_id):
        return False
    phase = str(rollout_phase or "").strip()
    return phase != "fleet_complete"


def normalize_live_open_gate(
    raw_gate: Any,
) -> dict[str, Any] | bool:
    if not isinstance(raw_gate, Mapping):
        return False
    mode = str(raw_gate.get("mode") or "").strip()
    release_id = str(raw_gate.get("release_id") or "").strip()
    rollout_phase = str(
        raw_gate.get("rollout_phase") or ""
    ).strip()
    phase_version = raw_gate.get("phase_version")
    if mode not in LIVE_OPEN_MODES:
        return False
    if not release_id or not rollout_phase:
        return False
    if isinstance(phase_version, bool) or not isinstance(
        phase_version,
        int,
    ):
        return False
    if phase_version < 1:
        return False
    if (
        mode == LIVE_OPEN_MODE_NORMAL
        and rollout_phase != LIVE_OPEN_NORMAL_PHASE
    ):
        return False
    if (
        mode == LIVE_OPEN_MODE_CANARY_ONLY
        and rollout_phase == LIVE_OPEN_NORMAL_PHASE
    ):
        return False
    return {
        "mode": mode,
        "release_id": release_id,
        "rollout_phase": rollout_phase,
        "phase_version": phase_version,
    }


def live_open_gate_denial(
    raw_gate: Any,
    *,
    trusted_gate: Any,
    expected_release_id: Any,
    require_normal: bool,
) -> str | None:
    if raw_gate in (None, False):
        return "live_open_gate_missing"
    gate = normalize_live_open_gate(raw_gate)
    if gate is False:
        return "live_open_gate_invalid"
    trusted = normalize_live_open_gate(trusted_gate)
    if trusted is False:
        return "live_open_gate_unavailable"
    release_id = str(expected_release_id or "").strip()
    if not release_id:
        return "live_open_gate_release_missing"
    if gate["release_id"] != release_id:
        return "live_open_gate_release_mismatch"
    if trusted["release_id"] != release_id:
        return "live_open_gate_heartbeat_release_mismatch"
    if require_normal and gate["mode"] != LIVE_OPEN_MODE_NORMAL:
        return "live_open_gate_not_normal"
    if gate != trusted:
        return "live_open_gate_mismatch"
    return None


class LiveCanaryExecutionState(str, Enum):
    RECEIVED = "received"
    CLAIMED = "claimed"
    DISPATCHED = "dispatched"
    EXCHANGE_CONFIRMED = "exchange_confirmed"
    LOSS_LIMIT_HALTED = "loss_limit_halted"
    CLOSE_DISPATCHED = "close_dispatched"
    CLOSED = "closed"


class LiveCanaryRegisterResult(str, Enum):
    REGISTERED = "registered"
    REPLAY = "replay"
    PERMIT_CONFLICT = "permit_conflict"


class LiveCanaryClaimResult(str, Enum):
    ACQUIRED = "acquired"
    RECOVERY_REQUIRED = "recovery_required"
    PERMIT_CONFLICT = "permit_conflict"


class LiveCanaryExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class LiveCanaryExecutionIdentity:
    permit_id: str
    release_id: str
    intent_id: str
    client_order_id: str
    account_id: str
    node_id: str
    symbol: str
    max_notional_usdt: str
    max_cumulative_loss_usdt: str
    authorized_limit_price_usdt: str
    expires_at: str = ""
    portfolio_baseline_sha256: str = ""

    def normalized(self) -> LiveCanaryExecutionIdentity:
        permit_id = str(UUID(str(self.permit_id)))
        intent_uuid = UUID(str(self.intent_id))
        release_id = str(self.release_id).strip()
        account_id = str(self.account_id).strip()
        node_id = str(self.node_id).strip()
        symbol = str(self.symbol).strip().upper()
        client_order_id = str(self.client_order_id).strip()
        expected_client_order_id = f"B{intent_uuid.hex}01"
        max_notional = _positive_decimal(
            self.max_notional_usdt,
            "max_notional_usdt",
        )
        max_cumulative_loss = _positive_decimal(
            self.max_cumulative_loss_usdt,
            "max_cumulative_loss_usdt",
        )
        authorized_limit_price = _positive_decimal(
            self.authorized_limit_price_usdt,
            "authorized_limit_price_usdt",
        )
        expires_at = _aware_timestamp(
            self.expires_at,
            "expires_at",
        )
        portfolio_baseline_sha256 = str(
            self.portfolio_baseline_sha256
        ).strip()
        if max_notional > Decimal("12"):
            raise LiveCanaryExecutionError(
                "max_notional_usdt exceeds hard cap"
            )
        if max_cumulative_loss >= Decimal("1.5"):
            raise LiveCanaryExecutionError(
                "max_cumulative_loss_usdt reaches hard cap"
            )
        if not release_id:
            raise LiveCanaryExecutionError("release_id is required")
        if not account_id:
            raise LiveCanaryExecutionError("account_id is required")
        if not node_id:
            raise LiveCanaryExecutionError("node_id is required")
        if not symbol:
            raise LiveCanaryExecutionError("symbol is required")
        if client_order_id != expected_client_order_id:
            raise LiveCanaryExecutionError(
                "client_order_id must be derived from intent_id"
            )
        if (
            re.fullmatch(
                r"[0-9a-f]{64}",
                portfolio_baseline_sha256,
            )
            is None
        ):
            raise LiveCanaryExecutionError(
                "portfolio_baseline_sha256 is invalid"
            )
        return LiveCanaryExecutionIdentity(
            permit_id=permit_id,
            release_id=release_id,
            intent_id=str(intent_uuid),
            client_order_id=client_order_id,
            account_id=account_id,
            node_id=node_id,
            symbol=symbol,
            max_notional_usdt=_canonical_decimal_text(max_notional),
            max_cumulative_loss_usdt=_canonical_decimal_text(
                max_cumulative_loss
            ),
            authorized_limit_price_usdt=_canonical_decimal_text(
                authorized_limit_price
            ),
            expires_at=expires_at.isoformat(),
            portfolio_baseline_sha256=portfolio_baseline_sha256,
        )


@dataclass(frozen=True)
class LiveCanaryExecutionRecord:
    permit_id: str
    release_id: str
    intent_id: str
    client_order_id: str
    account_id: str
    node_id: str
    symbol: str
    max_notional_usdt: str
    max_cumulative_loss_usdt: str
    authorized_limit_price_usdt: str
    expires_at: str
    portfolio_baseline_sha256: str
    state: LiveCanaryExecutionState
    updated_at: str
    instrument_id: str = ""
    processed_fill_ids: tuple[str, ...] = ()
    net_quantity: str = "0"
    average_entry_price_usdt: str = "0"
    fees_usdt: str = "0"
    realized_pnl_usdt: str = "0"
    current_loss_usdt: str = "0"
    peak_loss_usdt: str = "0"
    last_mark_price_usdt: str = "0"
    adverse_slippage_usdt: str = "0"
    close_client_order_id: str = ""
    close_side: str = ""
    close_required_quantity: str = "0"
    halt_reason: str = ""

    def identity(self) -> LiveCanaryExecutionIdentity:
        return LiveCanaryExecutionIdentity(
            permit_id=self.permit_id,
            release_id=self.release_id,
            intent_id=self.intent_id,
            client_order_id=self.client_order_id,
            account_id=self.account_id,
            node_id=self.node_id,
            symbol=self.symbol,
            max_notional_usdt=self.max_notional_usdt,
            max_cumulative_loss_usdt=self.max_cumulative_loss_usdt,
            authorized_limit_price_usdt=self.authorized_limit_price_usdt,
            expires_at=self.expires_at,
            portfolio_baseline_sha256=(
                self.portfolio_baseline_sha256
            ),
        )


@dataclass(frozen=True)
class LiveCanaryFill:
    fill_id: str
    client_order_id: str
    instrument_id: str
    side: str
    quantity: str
    price_usdt: str
    fee_usdt: str
    mark_price_usdt: str
    reduce_only: bool
    occurred_at: str
    accounting_error: str = ""

    def normalized(self) -> LiveCanaryFill:
        fill_id = str(self.fill_id).strip()
        client_order_id = str(self.client_order_id).strip()
        instrument_id = str(self.instrument_id).strip()
        side = str(self.side).strip().upper()
        occurred_at = str(self.occurred_at).strip()
        if not fill_id:
            raise LiveCanaryExecutionError("fill_id is required")
        if not client_order_id:
            raise LiveCanaryExecutionError(
                "fill client_order_id is required"
            )
        if not instrument_id:
            raise LiveCanaryExecutionError(
                "fill instrument_id is required"
            )
        if side not in {"BUY", "SELL"}:
            raise LiveCanaryExecutionError(
                "fill side must be BUY or SELL"
            )
        quantity = _positive_decimal(self.quantity, "fill quantity")
        price = _positive_decimal(self.price_usdt, "fill price_usdt")
        fee = _non_negative_decimal(self.fee_usdt, "fill fee_usdt")
        mark = _non_negative_decimal(
            self.mark_price_usdt,
            "fill mark_price_usdt",
        )
        if not occurred_at:
            raise LiveCanaryExecutionError(
                "fill occurred_at is required"
            )
        return LiveCanaryFill(
            fill_id=fill_id,
            client_order_id=client_order_id,
            instrument_id=instrument_id,
            side=side,
            quantity=format(quantity, "f"),
            price_usdt=format(price, "f"),
            fee_usdt=format(fee, "f"),
            mark_price_usdt=format(mark, "f"),
            reduce_only=bool(self.reduce_only),
            occurred_at=occurred_at,
            accounting_error=str(self.accounting_error).strip(),
        )


@dataclass(frozen=True)
class LiveCanaryMark:
    client_order_id: str
    instrument_id: str
    mark_price_usdt: str
    observed_at: str
    evaluated_at: str
    accounting_error: str = ""

    def normalized(self) -> LiveCanaryMark:
        client_order_id = str(self.client_order_id).strip()
        instrument_id = str(self.instrument_id).strip()
        if not client_order_id:
            raise LiveCanaryExecutionError(
                "mark client_order_id is required"
            )
        if not instrument_id:
            raise LiveCanaryExecutionError(
                "mark instrument_id is required"
            )
        mark_price = _non_negative_decimal(
            self.mark_price_usdt,
            "mark_price_usdt",
        )
        observed_at = _aware_timestamp(
            self.observed_at,
            "observed_at",
        )
        evaluated_at = _aware_timestamp(
            self.evaluated_at,
            "evaluated_at",
        )
        accounting_errors = []
        raw_error = str(self.accounting_error).strip()
        if raw_error:
            accounting_errors.append(raw_error)
        age_seconds = (evaluated_at - observed_at).total_seconds()
        if age_seconds < -1:
            accounting_errors.append(
                "mark price timestamp is in the future"
            )
        if age_seconds > 10:
            accounting_errors.append("mark price is stale")
        if mark_price <= 0:
            accounting_errors.append("mark price is unavailable")
        return LiveCanaryMark(
            client_order_id=client_order_id,
            instrument_id=instrument_id,
            mark_price_usdt=format(mark_price, "f"),
            observed_at=observed_at.isoformat(),
            evaluated_at=evaluated_at.isoformat(),
            accounting_error="; ".join(accounting_errors),
        )


@dataclass(frozen=True)
class LiveCanaryLossDecision:
    identity: LiveCanaryExecutionIdentity
    instrument_id: str
    current_loss_usdt: str
    peak_loss_usdt: str
    max_cumulative_loss_usdt: str
    close_client_order_id: str
    close_side: str
    close_quantity: str
    halt_reason: str


class JsonLiveCanaryExecutionStore:
    """Durable single-permit inbox for live canary executions."""

    _VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        self._process_lock = _process_lock_for(self._path)

    def register_received(
        self,
        identity: LiveCanaryExecutionIdentity,
    ) -> LiveCanaryRegisterResult:
        normalized = identity.normalized()

        def mutate(
            payload: dict[str, Any],
        ) -> tuple[LiveCanaryRegisterResult, bool]:
            record = self._record_for_claim(payload, normalized)
            if record is False:
                self._put_record(
                    payload,
                    normalized,
                    LiveCanaryExecutionState.RECEIVED,
                )
                return LiveCanaryRegisterResult.REGISTERED, True
            if not _same_identity(record.identity(), normalized):
                return LiveCanaryRegisterResult.PERMIT_CONFLICT, False
            return LiveCanaryRegisterResult.REPLAY, False

        return self._mutate(mutate)

    def claim(
        self,
        identity: LiveCanaryExecutionIdentity,
    ) -> LiveCanaryClaimResult:
        normalized = identity.normalized()

        def mutate(
            payload: dict[str, Any],
        ) -> tuple[LiveCanaryClaimResult, bool]:
            record = self._record_for_claim(payload, normalized)
            if record is False:
                self._put_record(
                    payload,
                    normalized,
                    LiveCanaryExecutionState.CLAIMED,
                )
                return LiveCanaryClaimResult.ACQUIRED, True
            if not _same_identity(record.identity(), normalized):
                return LiveCanaryClaimResult.PERMIT_CONFLICT, False
            if record.state is LiveCanaryExecutionState.RECEIVED:
                self._put_record(
                    payload,
                    normalized,
                    LiveCanaryExecutionState.CLAIMED,
                )
                return LiveCanaryClaimResult.ACQUIRED, True
            return LiveCanaryClaimResult.RECOVERY_REQUIRED, False

        return self._mutate(mutate)

    def mark_dispatched(
        self,
        identity: LiveCanaryExecutionIdentity,
    ) -> None:
        self._transition(
            identity,
            target=LiveCanaryExecutionState.DISPATCHED,
            allowed={
                LiveCanaryExecutionState.CLAIMED,
                LiveCanaryExecutionState.DISPATCHED,
                LiveCanaryExecutionState.EXCHANGE_CONFIRMED,
                LiveCanaryExecutionState.LOSS_LIMIT_HALTED,
                LiveCanaryExecutionState.CLOSE_DISPATCHED,
                LiveCanaryExecutionState.CLOSED,
            },
        )

    def mark_exchange_confirmed(
        self,
        identity: LiveCanaryExecutionIdentity,
    ) -> None:
        self._transition(
            identity,
            target=LiveCanaryExecutionState.EXCHANGE_CONFIRMED,
            allowed={
                LiveCanaryExecutionState.CLAIMED,
                LiveCanaryExecutionState.DISPATCHED,
                LiveCanaryExecutionState.EXCHANGE_CONFIRMED,
                LiveCanaryExecutionState.LOSS_LIMIT_HALTED,
                LiveCanaryExecutionState.CLOSE_DISPATCHED,
                LiveCanaryExecutionState.CLOSED,
            },
        )

    def mark_exchange_confirmed_by_client_order_id(
        self,
        client_order_id: str,
    ) -> bool:
        target = str(client_order_id).strip()
        if not target:
            return False

        def mutate(payload: dict[str, Any]) -> tuple[bool, bool]:
            match = self._record_entry_for_client_order_id(payload, target)
            if match is False:
                return False, False
            key, record = match
            if record.state in {
                LiveCanaryExecutionState.EXCHANGE_CONFIRMED,
                LiveCanaryExecutionState.LOSS_LIMIT_HALTED,
                LiveCanaryExecutionState.CLOSE_DISPATCHED,
                LiveCanaryExecutionState.CLOSED,
            }:
                return True, False
            if record.state not in {
                LiveCanaryExecutionState.CLAIMED,
                LiveCanaryExecutionState.DISPATCHED,
            }:
                raise LiveCanaryExecutionError(
                    "invalid exchange confirmation transition"
                )
            updated = replace(
                record,
                state=LiveCanaryExecutionState.EXCHANGE_CONFIRMED,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            payload["records"][key] = _serialize_record(updated)
            return True, True

        return self._mutate(mutate)

    def get(
        self,
        identity: LiveCanaryExecutionIdentity,
    ) -> LiveCanaryExecutionRecord | Literal[False]:
        normalized = identity.normalized()

        def read(
            payload: dict[str, Any],
        ) -> LiveCanaryExecutionRecord | Literal[False]:
            record = self._record_for_permit(payload, normalized)
            if record is False:
                return False
            if not _same_identity(record.identity(), normalized):
                return False
            return record

        return self._read_locked(read)

    def find_by_client_order_id(
        self,
        client_order_id: str,
    ) -> LiveCanaryExecutionRecord | Literal[False]:
        target = str(client_order_id).strip()
        if not target:
            return False

        def read(
            payload: dict[str, Any],
        ) -> LiveCanaryExecutionRecord | Literal[False]:
            for raw in payload["records"].values():
                record = self._record_from_raw(raw)
                if target in {
                    record.client_order_id,
                    record.close_client_order_id,
                }:
                    return record
            return False

        return self._read_locked(read)

    def record_fill(
        self,
        fill: LiveCanaryFill,
    ) -> LiveCanaryLossDecision | Literal[False]:
        normalized_fill = fill.normalized()

        def mutate(
            payload: dict[str, Any],
        ) -> tuple[LiveCanaryLossDecision | Literal[False], bool]:
            match = self._record_entry_for_client_order_id(
                payload,
                normalized_fill.client_order_id,
            )
            if match is False:
                return False, False
            key, record = match
            if normalized_fill.fill_id in record.processed_fill_ids:
                return _loss_decision(record, normalized_fill.instrument_id), False
            updated = _record_with_fill(record, normalized_fill)
            payload["records"][key] = _serialize_record(updated)
            return _loss_decision(updated, normalized_fill.instrument_id), True

        return self._mutate(mutate)

    def record_mark(
        self,
        mark: LiveCanaryMark,
    ) -> LiveCanaryLossDecision | Literal[False]:
        normalized_mark = mark.normalized()

        def mutate(
            payload: dict[str, Any],
        ) -> tuple[LiveCanaryLossDecision | Literal[False], bool]:
            match = self._record_entry_for_client_order_id(
                payload,
                normalized_mark.client_order_id,
            )
            if match is False:
                return False, False
            key, record = match
            if record.state is LiveCanaryExecutionState.CLOSED:
                return False, False
            if Decimal(record.net_quantity) == 0:
                return False, False
            updated = _record_with_mark(record, normalized_mark)
            payload["records"][key] = _serialize_record(updated)
            return _loss_decision(
                updated,
                normalized_mark.instrument_id,
            ), True

        return self._mutate(mutate)

    def active_monitor_targets(
        self,
    ) -> tuple[tuple[str, str], ...]:
        return tuple(
            (client_order_id, instrument_id)
            for (
                client_order_id,
                instrument_id,
                _symbol,
                _portfolio_baseline,
            ) in self.active_monitor_contexts()
        )

    def active_monitor_contexts(
        self,
    ) -> tuple[tuple[str, str, str, str], ...]:
        def read(
            payload: dict[str, Any],
        ) -> tuple[tuple[str, str, str, str], ...]:
            targets = []
            for raw in payload["records"].values():
                record = self._record_from_raw(raw)
                if record.state is LiveCanaryExecutionState.CLOSED:
                    continue
                if Decimal(record.net_quantity) == 0:
                    continue
                instrument_id = record.instrument_id
                if not instrument_id:
                    instrument_id = f"{record.symbol}-PERP.BINANCE"
                targets.append(
                    (
                        record.client_order_id,
                        instrument_id,
                        record.symbol,
                        record.portfolio_baseline_sha256,
                    )
                )
            targets.sort()
            return tuple(targets)

        return self._read_locked(read)

    def mark_close_dispatched(
        self,
        identity: LiveCanaryExecutionIdentity,
    ) -> None:
        normalized = identity.normalized()

        def mutate(payload: dict[str, Any]) -> tuple[None, bool]:
            record = self._record_for_permit(payload, normalized)
            if record is False:
                raise LiveCanaryExecutionError(
                    "execution claim does not exist"
                )
            if not _same_identity(record.identity(), normalized):
                raise LiveCanaryExecutionError(
                    "execution identity conflicts with claimed permit"
                )
            if record.state is LiveCanaryExecutionState.CLOSED:
                return None, False
            if record.state is LiveCanaryExecutionState.CLOSE_DISPATCHED:
                return None, False
            if record.state is not LiveCanaryExecutionState.LOSS_LIMIT_HALTED:
                raise LiveCanaryExecutionError(
                    "live canary close is not pending"
                )
            updated = replace(
                record,
                state=LiveCanaryExecutionState.CLOSE_DISPATCHED,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            payload["records"][_permit_key(normalized)] = (
                _serialize_record(updated)
            )
            return None, True

        self._mutate(mutate)

    def pending_close_decisions(
        self,
    ) -> tuple[LiveCanaryLossDecision, ...]:
        def read(
            payload: dict[str, Any],
        ) -> tuple[LiveCanaryLossDecision, ...]:
            decisions = []
            for raw in payload["records"].values():
                record = self._record_from_raw(raw)
                decision = _loss_decision(record, "")
                if decision is not False:
                    decisions.append(decision)
            return tuple(decisions)

        return self._read_locked(read)

    def _record_entry_for_client_order_id(
        self,
        payload: dict[str, Any],
        client_order_id: str,
    ) -> tuple[str, LiveCanaryExecutionRecord] | Literal[False]:
        for key, raw in payload["records"].items():
            record = self._record_from_raw(raw)
            if client_order_id in {
                record.client_order_id,
                record.close_client_order_id,
            }:
                return key, record
        return False

    def _transition(
        self,
        identity: LiveCanaryExecutionIdentity,
        *,
        target: LiveCanaryExecutionState,
        allowed: set[LiveCanaryExecutionState],
    ) -> None:
        normalized = identity.normalized()

        def mutate(payload: dict[str, Any]) -> tuple[None, bool]:
            record = self._record_for_permit(payload, normalized)
            if record is False:
                raise LiveCanaryExecutionError(
                    "execution claim does not exist"
                )
            if not _same_identity(record.identity(), normalized):
                raise LiveCanaryExecutionError(
                    "execution identity conflicts with claimed permit"
                )
            if record.state not in allowed:
                raise LiveCanaryExecutionError(
                    f"invalid execution transition {record.state.value}"
                )
            if record.state in {
                LiveCanaryExecutionState.EXCHANGE_CONFIRMED,
                LiveCanaryExecutionState.LOSS_LIMIT_HALTED,
                LiveCanaryExecutionState.CLOSE_DISPATCHED,
                LiveCanaryExecutionState.CLOSED,
            }:
                return None, False
            if record.state is target:
                return None, False
            self._put_record(payload, normalized, target)
            return None, True

        self._mutate(mutate)

    def _mutate(
        self,
        operation: Callable[[dict[str, Any]], tuple[Any, bool]],
    ) -> Any:
        with self._process_lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock_path.open("a+", encoding="utf-8") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                payload = self._load_payload()
                result, changed = operation(payload)
                if changed:
                    self._save_payload(payload)
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                return result

    def _read_locked(
        self,
        operation: Callable[[dict[str, Any]], Any],
    ) -> Any:
        with self._process_lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock_path.open("a+", encoding="utf-8") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
                payload = self._load_payload()
                result = operation(payload)
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                return result

    def _load_payload(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"version": self._VERSION, "records": {}}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LiveCanaryExecutionError(
                "live canary execution inbox is unreadable"
            ) from exc
        if not isinstance(payload, dict):
            raise LiveCanaryExecutionError(
                "live canary execution inbox root must be an object"
            )
        if payload.get("version") != self._VERSION:
            raise LiveCanaryExecutionError(
                "unsupported live canary execution inbox version"
            )
        records = payload.get("records")
        if not isinstance(records, dict):
            raise LiveCanaryExecutionError(
                "live canary execution records must be an object"
            )
        return payload

    def _save_payload(self, payload: dict[str, Any]) -> None:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            dir=str(self._path.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                json.dump(
                    payload,
                    tmp,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                tmp.write("\n")
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, self._path)
            _fsync_directory(self._path.parent)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def _record_for_permit(
        self,
        payload: dict[str, Any],
        identity: LiveCanaryExecutionIdentity,
    ) -> LiveCanaryExecutionRecord | Literal[False]:
        records = payload["records"]
        raw = records.get(_permit_key(identity))
        if raw is None:
            return False
        return self._record_from_raw(raw)

    def _record_for_claim(
        self,
        payload: dict[str, Any],
        identity: LiveCanaryExecutionIdentity,
    ) -> LiveCanaryExecutionRecord | Literal[False]:
        exact = self._record_for_permit(payload, identity)
        if exact is not False:
            return exact
        for raw in payload["records"].values():
            record = self._record_from_raw(raw)
            if record.permit_id == identity.permit_id:
                return record
            if record.release_id == identity.release_id:
                return record
        return False

    def _record_from_raw(
        self,
        raw: Any,
    ) -> LiveCanaryExecutionRecord:
        if not isinstance(raw, dict):
            raise LiveCanaryExecutionError(
                "live canary execution record must be an object"
            )
        try:
            return LiveCanaryExecutionRecord(
                permit_id=str(raw["permit_id"]),
                release_id=str(raw["release_id"]),
                intent_id=str(raw["intent_id"]),
                client_order_id=str(raw["client_order_id"]),
                account_id=str(raw["account_id"]),
                node_id=str(raw["node_id"]),
                symbol=str(raw["symbol"]),
                max_notional_usdt=str(raw["max_notional_usdt"]),
                max_cumulative_loss_usdt=str(
                    raw.get("max_cumulative_loss_usdt", "1.5")
                ),
                authorized_limit_price_usdt=str(
                    raw.get("authorized_limit_price_usdt", "1")
                ),
                expires_at=str(raw.get("expires_at", "")),
                portfolio_baseline_sha256=str(
                    raw.get("portfolio_baseline_sha256", "")
                ),
                state=LiveCanaryExecutionState(str(raw["state"])),
                updated_at=str(raw["updated_at"]),
                instrument_id=str(raw.get("instrument_id", "")),
                processed_fill_ids=tuple(
                    str(value)
                    for value in raw.get("processed_fill_ids", ())
                ),
                net_quantity=str(raw.get("net_quantity", "0")),
                average_entry_price_usdt=str(
                    raw.get("average_entry_price_usdt", "0")
                ),
                fees_usdt=str(raw.get("fees_usdt", "0")),
                realized_pnl_usdt=str(
                    raw.get("realized_pnl_usdt", "0")
                ),
                current_loss_usdt=str(
                    raw.get("current_loss_usdt", "0")
                ),
                peak_loss_usdt=str(raw.get("peak_loss_usdt", "0")),
                last_mark_price_usdt=str(
                    raw.get("last_mark_price_usdt", "0")
                ),
                adverse_slippage_usdt=str(
                    raw.get("adverse_slippage_usdt", "0")
                ),
                close_client_order_id=str(
                    raw.get("close_client_order_id", "")
                ),
                close_side=str(raw.get("close_side", "")),
                close_required_quantity=str(
                    raw.get("close_required_quantity", "0")
                ),
                halt_reason=str(raw.get("halt_reason", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LiveCanaryExecutionError(
                "live canary execution record is invalid"
            ) from exc

    def _put_record(
        self,
        payload: dict[str, Any],
        identity: LiveCanaryExecutionIdentity,
        state: LiveCanaryExecutionState,
    ) -> None:
        record = LiveCanaryExecutionRecord(
            permit_id=identity.permit_id,
            release_id=identity.release_id,
            intent_id=identity.intent_id,
            client_order_id=identity.client_order_id,
            account_id=identity.account_id,
            node_id=identity.node_id,
            symbol=identity.symbol,
            max_notional_usdt=identity.max_notional_usdt,
            max_cumulative_loss_usdt=(
                identity.max_cumulative_loss_usdt
            ),
            authorized_limit_price_usdt=(
                identity.authorized_limit_price_usdt
            ),
            expires_at=identity.expires_at,
            portfolio_baseline_sha256=(
                identity.portfolio_baseline_sha256
            ),
            state=state,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        payload["records"][_permit_key(identity)] = _serialize_record(record)


def deterministic_canary_client_order_id(intent_id: Any) -> str:
    intent_uuid = UUID(str(intent_id))
    return f"B{intent_uuid.hex}01"


def deterministic_canary_close_client_order_id(intent_id: Any) -> str:
    intent_uuid = UUID(str(intent_id))
    return f"B{intent_uuid.hex}99"


def _record_with_fill(
    record: LiveCanaryExecutionRecord,
    fill: LiveCanaryFill,
) -> LiveCanaryExecutionRecord:
    quantity = Decimal(fill.quantity)
    price = Decimal(fill.price_usdt)
    fee = Decimal(fill.fee_usdt)
    mark = Decimal(fill.mark_price_usdt)
    net_quantity = Decimal(record.net_quantity)
    average_entry_price = Decimal(record.average_entry_price_usdt)
    fees = Decimal(record.fees_usdt) + fee
    realized_pnl = Decimal(record.realized_pnl_usdt)
    adverse_slippage = Decimal(record.adverse_slippage_usdt)
    accounting_errors = []
    if fill.accounting_error:
        accounting_errors.append(fill.accounting_error)

    signed_fill = quantity
    if fill.side == "SELL":
        signed_fill = -quantity

    if fill.reduce_only:
        if net_quantity == 0:
            accounting_errors.append(
                "reduce-only fill arrived without an open canary quantity"
            )
        elif net_quantity * signed_fill >= 0:
            accounting_errors.append(
                "reduce-only fill side does not reduce canary quantity"
            )
        elif quantity > abs(net_quantity):
            accounting_errors.append(
                "reduce-only fill exceeds canary open quantity"
            )
        else:
            closing_quantity = min(quantity, abs(net_quantity))
            if net_quantity > 0:
                realized_pnl += (
                    price - average_entry_price
                ) * closing_quantity
            else:
                realized_pnl += (
                    average_entry_price - price
                ) * closing_quantity
            net_quantity += signed_fill
            if net_quantity == 0:
                average_entry_price = Decimal("0")
    else:
        if net_quantity != 0 and net_quantity * signed_fill < 0:
            accounting_errors.append(
                "canary entry fill reverses the tracked position"
            )
        else:
            previous_quantity = abs(net_quantity)
            total_quantity = previous_quantity + quantity
            weighted_quote = (
                average_entry_price * previous_quantity
                + price * quantity
            )
            average_entry_price = weighted_quote / total_quantity
            net_quantity += signed_fill
            if mark > 0:
                mark_slippage = _adverse_slippage(
                    side=fill.side,
                    fill_price=price,
                    mark_price=mark,
                    quantity=quantity,
                )
                authorized_slippage = _adverse_slippage(
                    side=fill.side,
                    fill_price=price,
                    mark_price=Decimal(
                        record.authorized_limit_price_usdt
                    ),
                    quantity=quantity,
                )
                adverse_slippage += max(
                    mark_slippage,
                    authorized_slippage,
                )

    if mark <= 0:
        accounting_errors.append("mark price is unavailable")
    unrealized_pnl = _unrealized_pnl(
        net_quantity=net_quantity,
        average_entry_price=average_entry_price,
        mark_price=mark,
    )
    mark_to_market_loss = max(
        Decimal("0"),
        fees - realized_pnl - unrealized_pnl,
    )
    execution_loss = max(
        Decimal("0"),
        fees + adverse_slippage - realized_pnl,
    )
    current_loss = max(mark_to_market_loss, execution_loss)
    peak_loss = max(
        Decimal(record.peak_loss_usdt),
        current_loss,
    )
    max_cumulative_loss = Decimal(
        record.max_cumulative_loss_usdt
    )
    halt_reason = record.halt_reason
    if accounting_errors and not halt_reason:
        halt_reason = "live canary accounting incomplete: " + "; ".join(
            accounting_errors
        )
    if peak_loss >= max_cumulative_loss and not halt_reason:
        halt_reason = (
            "live canary cumulative loss limit reached: "
            f"loss={format(peak_loss, 'f')}:"
            f"limit={format(max_cumulative_loss, 'f')}"
        )

    state = LiveCanaryExecutionState.EXCHANGE_CONFIRMED
    close_client_order_id = record.close_client_order_id
    close_side = record.close_side
    close_required_quantity = Decimal("0")
    if halt_reason:
        if net_quantity == 0:
            state = LiveCanaryExecutionState.CLOSED
        elif record.state is LiveCanaryExecutionState.CLOSE_DISPATCHED:
            state = LiveCanaryExecutionState.CLOSE_DISPATCHED
        else:
            state = LiveCanaryExecutionState.LOSS_LIMIT_HALTED
        close_client_order_id = deterministic_canary_close_client_order_id(
            record.intent_id
        )
        close_required_quantity = abs(net_quantity)
        close_side = "SELL"
        if net_quantity < 0:
            close_side = "BUY"

    processed_fill_ids = record.processed_fill_ids + (fill.fill_id,)
    return replace(
        record,
        state=state,
        updated_at=datetime.now(timezone.utc).isoformat(),
        instrument_id=fill.instrument_id,
        processed_fill_ids=processed_fill_ids,
        net_quantity=format(net_quantity, "f"),
        average_entry_price_usdt=format(
            average_entry_price,
            "f",
        ),
        fees_usdt=format(fees, "f"),
        realized_pnl_usdt=format(realized_pnl, "f"),
        current_loss_usdt=format(current_loss, "f"),
        peak_loss_usdt=format(peak_loss, "f"),
        last_mark_price_usdt=format(mark, "f"),
        adverse_slippage_usdt=format(adverse_slippage, "f"),
        close_client_order_id=close_client_order_id,
        close_side=close_side,
        close_required_quantity=format(
            close_required_quantity,
            "f",
        ),
        halt_reason=halt_reason,
    )


def _record_with_mark(
    record: LiveCanaryExecutionRecord,
    mark: LiveCanaryMark,
) -> LiveCanaryExecutionRecord:
    mark_price = Decimal(mark.mark_price_usdt)
    net_quantity = Decimal(record.net_quantity)
    average_entry_price = Decimal(record.average_entry_price_usdt)
    fees = Decimal(record.fees_usdt)
    realized_pnl = Decimal(record.realized_pnl_usdt)
    adverse_slippage = Decimal(record.adverse_slippage_usdt)
    effective_mark = mark_price
    if effective_mark <= 0:
        effective_mark = Decimal(record.last_mark_price_usdt)
    unrealized_pnl = _unrealized_pnl(
        net_quantity=net_quantity,
        average_entry_price=average_entry_price,
        mark_price=effective_mark,
    )
    mark_to_market_loss = max(
        Decimal("0"),
        fees - realized_pnl - unrealized_pnl,
    )
    execution_loss = max(
        Decimal("0"),
        fees + adverse_slippage - realized_pnl,
    )
    current_loss = max(mark_to_market_loss, execution_loss)
    peak_loss = max(
        Decimal(record.peak_loss_usdt),
        current_loss,
    )
    halt_reason = record.halt_reason
    if mark.accounting_error and not halt_reason:
        halt_reason = (
            "live canary accounting incomplete: "
            + mark.accounting_error
        )
    max_cumulative_loss = Decimal(
        record.max_cumulative_loss_usdt
    )
    if peak_loss >= max_cumulative_loss and not halt_reason:
        halt_reason = (
            "live canary cumulative loss limit reached: "
            f"loss={format(peak_loss, 'f')}:"
            f"limit={format(max_cumulative_loss, 'f')}"
        )

    state = LiveCanaryExecutionState.EXCHANGE_CONFIRMED
    close_client_order_id = record.close_client_order_id
    close_side = record.close_side
    close_required_quantity = Decimal("0")
    if halt_reason:
        if record.state is LiveCanaryExecutionState.CLOSE_DISPATCHED:
            state = LiveCanaryExecutionState.CLOSE_DISPATCHED
        else:
            state = LiveCanaryExecutionState.LOSS_LIMIT_HALTED
        close_client_order_id = deterministic_canary_close_client_order_id(
            record.intent_id
        )
        close_required_quantity = abs(net_quantity)
        close_side = "SELL"
        if net_quantity < 0:
            close_side = "BUY"

    last_mark_price = record.last_mark_price_usdt
    if mark_price > 0:
        last_mark_price = format(mark_price, "f")
    return replace(
        record,
        state=state,
        updated_at=datetime.now(timezone.utc).isoformat(),
        instrument_id=mark.instrument_id,
        current_loss_usdt=format(current_loss, "f"),
        peak_loss_usdt=format(peak_loss, "f"),
        last_mark_price_usdt=last_mark_price,
        close_client_order_id=close_client_order_id,
        close_side=close_side,
        close_required_quantity=format(
            close_required_quantity,
            "f",
        ),
        halt_reason=halt_reason,
    )


def _loss_decision(
    record: LiveCanaryExecutionRecord,
    instrument_id: str,
) -> LiveCanaryLossDecision | Literal[False]:
    if record.state not in {
        LiveCanaryExecutionState.LOSS_LIMIT_HALTED,
        LiveCanaryExecutionState.CLOSE_DISPATCHED,
    }:
        return False
    close_quantity = Decimal(record.close_required_quantity)
    if close_quantity <= 0:
        return False
    resolved_instrument_id = str(instrument_id).strip()
    if not resolved_instrument_id:
        resolved_instrument_id = record.instrument_id
    if not resolved_instrument_id:
        resolved_instrument_id = f"{record.symbol}-PERP.BINANCE"
    return LiveCanaryLossDecision(
        identity=record.identity(),
        instrument_id=resolved_instrument_id,
        current_loss_usdt=record.current_loss_usdt,
        peak_loss_usdt=record.peak_loss_usdt,
        max_cumulative_loss_usdt=record.max_cumulative_loss_usdt,
        close_client_order_id=record.close_client_order_id,
        close_side=record.close_side,
        close_quantity=record.close_required_quantity,
        halt_reason=record.halt_reason,
    )


def _unrealized_pnl(
    *,
    net_quantity: Decimal,
    average_entry_price: Decimal,
    mark_price: Decimal,
) -> Decimal:
    if net_quantity == 0 or mark_price <= 0:
        return Decimal("0")
    if net_quantity > 0:
        return (
            mark_price - average_entry_price
        ) * net_quantity
    return (
        average_entry_price - mark_price
    ) * abs(net_quantity)


def _adverse_slippage(
    *,
    side: str,
    fill_price: Decimal,
    mark_price: Decimal,
    quantity: Decimal,
) -> Decimal:
    adverse_price = fill_price - mark_price
    if side == "SELL":
        adverse_price = mark_price - fill_price
    return max(Decimal("0"), adverse_price) * quantity


def _serialize_record(
    record: LiveCanaryExecutionRecord,
) -> dict[str, Any]:
    serialized = asdict(record)
    serialized["state"] = record.state.value
    serialized["processed_fill_ids"] = list(record.processed_fill_ids)
    return serialized


def _process_lock_for(path: Path) -> RLock:
    key = str(path.resolve())
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _PROCESS_LOCKS[key] = lock
        return lock


def _permit_key(identity: LiveCanaryExecutionIdentity) -> str:
    return f"{identity.release_id}:{identity.permit_id}"


def _same_identity(
    left: LiveCanaryExecutionIdentity,
    right: LiveCanaryExecutionIdentity,
) -> bool:
    return left.normalized() == right.normalized()


def _canonical_decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _positive_decimal(value: Any, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise LiveCanaryExecutionError(f"{label} must be a decimal") from exc
    if not number.is_finite() or number <= 0:
        raise LiveCanaryExecutionError(f"{label} must be positive")
    return number


def _non_negative_decimal(value: Any, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise LiveCanaryExecutionError(f"{label} must be a decimal") from exc
    if not number.is_finite() or number < 0:
        raise LiveCanaryExecutionError(
            f"{label} must be non-negative"
        )
    return number


def _aware_timestamp(value: Any, label: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise LiveCanaryExecutionError(f"{label} is required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LiveCanaryExecutionError(
            f"{label} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise LiveCanaryExecutionError(
            f"{label} must include a timezone"
        )
    return parsed.astimezone(timezone.utc)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
