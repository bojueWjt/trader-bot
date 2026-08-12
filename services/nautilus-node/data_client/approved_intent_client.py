from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol
from uuid import UUID

from pydantic import ValidationError

from execution_domain.contracts import (
    ApprovedTradeIntentV1,
    IntentAction,
)
from execution_domain.control_plane import (
    ControlPlaneIntentSource,
    IntentAckStatus,
    IntentItem,
    TradingState,
)
from runtime.live_canary_execution import (
    JsonLiveCanaryExecutionStore,
    LiveCanaryExecutionIdentity,
    LiveCanaryExecutionState,
    LiveCanaryRegisterResult,
    deterministic_canary_client_order_id,
    is_live_canary_account,
    live_canary_permit_required,
    live_open_gate_denial,
    normalize_live_open_gate,
)
from runtime.intent_execution_inbox import (
    IntentExecutionIdentity,
    IntentExecutionState,
    IntentRegisterResult,
    JsonIntentExecutionInbox,
)


class IntentPublisher(Protocol):
    def publish(self, intent: ApprovedTradeIntentV1) -> None: ...


@dataclass
class IntentOffsetState:
    last_cursor: Optional[str] = None
    processed_intents: set[str] = field(default_factory=set)
    processed_idempotency_keys: set[str] = field(default_factory=set)

    def intent_key(self, intent: ApprovedTradeIntentV1) -> str:
        return f"{intent.account_id}:{intent.intent_id}"

    def has_processed(self, intent: ApprovedTradeIntentV1) -> Optional[str]:
        if self.intent_key(intent) in self.processed_intents:
            return "duplicate_intent"
        if intent.idempotency_key in self.processed_idempotency_keys:
            return "duplicate_idempotency_key"
        return None

    def record_processed(self, intent: ApprovedTradeIntentV1) -> None:
        self.processed_intents.add(self.intent_key(intent))
        self.processed_idempotency_keys.add(intent.idempotency_key)


class JsonIntentOffsetStore:
    """Small durable cursor store for one account/node intent stream."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> IntentOffsetState:
        if not self._path.exists():
            return IntentOffsetState()
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        return IntentOffsetState(
            last_cursor=raw.get("last_cursor"),
            processed_intents=set(raw.get("processed_intents", [])),
            processed_idempotency_keys=set(raw.get("processed_idempotency_keys", [])),
        )

    def save(self, state: IntentOffsetState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_cursor": state.last_cursor,
            "processed_intents": sorted(state.processed_intents),
            "processed_idempotency_keys": sorted(state.processed_idempotency_keys),
        }
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            dir=str(self._path.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                json.dump(payload, tmp, sort_keys=True)
                tmp.write("\n")
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, self._path)
            _fsync_directory(self._path.parent)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)


class ApprovedIntentDataClient:
    """Consumes approved trade intents from control-plane into Nautilus data."""

    _NEW_POSITION_ACTIONS = {
        IntentAction.OPEN_POSITION,
        IntentAction.ADD_POSITION,
    }

    def __init__(
        self,
        account_id: str,
        node_id: str,
        source: ControlPlaneIntentSource,
        publisher: IntentPublisher,
        offset_store: JsonIntentOffsetStore,
        now: Optional[Callable[[], datetime]] = None,
        trading_state: Optional[Callable[[], TradingState]] = None,
        live_canary_release_id: str | None = None,
        live_canary_execution_path: str | Path | None = None,
        intent_execution_inbox_path: str | Path | None = None,
        live_canary_portfolio_baseline: Optional[
            Callable[[str], str | bool]
        ] = None,
        live_rollout_phase: Optional[Callable[[], str | None]] = None,
        live_open_gate: Optional[
            Callable[[], Mapping[str, Any] | bool]
        ] = None,
    ) -> None:
        self._account_id = account_id
        self._node_id = node_id
        self._source = source
        self._publisher = publisher
        self._offset_store = offset_store
        self._state = offset_store.load()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._trading_state = trading_state or (lambda: TradingState.HALTED)
        self._live_canary_release_id = str(
            live_canary_release_id or ""
        ).strip()
        self._live_canary_portfolio_baseline = (
            live_canary_portfolio_baseline
        )
        self._live_rollout_phase = live_rollout_phase or (lambda: None)
        self._live_open_gate = live_open_gate or (lambda: False)
        execution_path = live_canary_execution_path
        if execution_path is None:
            execution_path = Path(
                os.environ.get("NODE_STATE_DIR") or "/state"
            ) / "live_canary_execution.json"
        self._live_canary_execution_store = (
            JsonLiveCanaryExecutionStore(execution_path)
        )
        inbox_path = intent_execution_inbox_path
        if inbox_path is None:
            inbox_path = offset_store.path.with_name(
                "intent_execution_inbox.json"
            )
        self._intent_execution_inbox = JsonIntentExecutionInbox(
            inbox_path
        )
        self._startup_replayed = False
        self._startup_replayed_intent_ids: set[str] = set()

    @property
    def state(self) -> IntentOffsetState:
        return self._state

    def poll_once(self, limit: int = 100, wait_ms: int = 0) -> int:
        replayed = self._replay_pending_once()
        items = self.fetch_once(limit=limit, wait_ms=wait_ms)
        for item in items:
            intent_id = _extract_intent_id(item)
            if (
                intent_id is not None
                and str(intent_id)
                in self._startup_replayed_intent_ids
            ):
                self._advance_cursor(item.cursor)
                continue
            self.deliver(item)
        if items:
            return len(items)
        return replayed

    def replay_pending(self) -> int:
        replayed = 0
        self._startup_replayed_intent_ids.clear()
        for record in self._intent_execution_inbox.pending():
            if record.account_id != self._account_id:
                continue
            try:
                intent = ApprovedTradeIntentV1.model_validate(
                    record.intent_payload
                )
            except (TypeError, ValueError, ValidationError):
                self._intent_execution_inbox.mark_rejected(
                    record.identity(),
                    "schema_mismatch",
                )
                continue
            if record.state is IntentExecutionState.RECEIVED:
                rejection = self._pending_received_rejection(intent)
                if rejection is not None:
                    status, detail = rejection
                    self._intent_execution_inbox.mark_rejected(
                        record.identity(),
                        detail,
                    )
                    self._ack(intent.intent_id, status, detail)
                    self._state.record_processed(intent)
                    self._offset_store.save(self._state)
                    self._startup_replayed_intent_ids.add(
                        str(intent.intent_id)
                    )
                    replayed += 1
                    continue
            self._publisher.publish(intent)
            self._state.record_processed(intent)
            self._offset_store.save(self._state)
            canary_identity = self._live_canary_identity(intent)
            if canary_identity is False:
                self._ack(
                    intent.intent_id,
                    IntentAckStatus.ACCEPTED,
                    "durable_replay",
                )
            else:
                status, detail = self._live_canary_ack(
                    canary_identity
                )
                self._ack(intent.intent_id, status, detail)
            self._startup_replayed_intent_ids.add(
                str(intent.intent_id)
            )
            replayed += 1
        return replayed

    def _replay_pending_once(self) -> int:
        if self._startup_replayed:
            return 0
        replayed = self.replay_pending()
        self._startup_replayed = True
        return replayed

    def fetch_once(
        self,
        limit: int = 100,
        wait_ms: int = 0,
    ) -> tuple[IntentItem, ...]:
        batch = self._source.fetch_intents(
            account_id=self._account_id,
            after_cursor=self._state.last_cursor,
            limit=limit,
            wait_ms=wait_ms,
        )
        return tuple(batch.items)

    def deliver(self, item: IntentItem) -> None:
        self._process_item(item)

    def _process_item(self, item: IntentItem) -> None:
        intent_id = _extract_intent_id(item)
        intent = self._validate_intent(item)
        if intent is None:
            if intent_id is not None:
                self._ack(intent_id, IntentAckStatus.REJECTED, "schema_mismatch")
            self._advance_cursor(item.cursor)
            return

        if intent.account_id != self._account_id:
            self._ack(intent.intent_id, IntentAckStatus.REJECTED, "wrong_account")
            self._advance_cursor(item.cursor)
            return

        execution_identity = _intent_execution_identity(intent)
        durable_record = self._intent_execution_inbox.get(
            execution_identity
        )
        if durable_record is not False:
            if (
                durable_record.state
                is IntentExecutionState.EXCHANGE_CONFIRMED
            ):
                self._state.record_processed(intent)
                self._advance_cursor(item.cursor)
                self._ack(
                    intent.intent_id,
                    IntentAckStatus.DUPLICATE,
                    "durable_exchange_confirmed",
                )
                return
            if durable_record.state is IntentExecutionState.REJECTED:
                self._state.record_processed(intent)
                self._advance_cursor(item.cursor)
                self._ack(
                    intent.intent_id,
                    IntentAckStatus.DUPLICATE,
                    "durable_rejected",
                )
                return

        if _as_aware(intent.valid_until) <= self._now_aware():
            self._ack(intent.intent_id, IntentAckStatus.EXPIRED, "expired")
            self._advance_cursor(item.cursor)
            return

        duplicate_detail = self._state.has_processed(intent)
        if duplicate_detail is not None:
            self._ack(intent.intent_id, IntentAckStatus.DUPLICATE, duplicate_detail)
            self._advance_cursor(item.cursor)
            return

        if (
            TradingState(self._trading_state()) is TradingState.HALTED
            and intent.action in self._NEW_POSITION_ACTIONS
        ):
            self._ack(intent.intent_id, IntentAckStatus.REJECTED, "halted")
            self._advance_cursor(item.cursor)
            return

        canary_denial = self._live_canary_denial(intent)
        if canary_denial is not None:
            self._ack(
                intent.intent_id,
                IntentAckStatus.REJECTED,
                canary_denial,
            )
            self._advance_cursor(item.cursor)
            return

        canary_identity = self._live_canary_identity(intent)
        if canary_identity is not False:
            register_result = (
                self._live_canary_execution_store.register_received(
                    canary_identity
                )
            )
            if (
                register_result
                is LiveCanaryRegisterResult.PERMIT_CONFLICT
            ):
                self._ack(
                    intent.intent_id,
                    IntentAckStatus.REJECTED,
                    "canary_permit_already_claimed",
                )
                self._advance_cursor(item.cursor)
                return

        register_result = self._intent_execution_inbox.register_received(
            execution_identity,
            intent.model_dump(mode="json"),
        )
        if register_result is IntentRegisterResult.INTENT_CONFLICT:
            self._ack(
                intent.intent_id,
                IntentAckStatus.DUPLICATE,
                "duplicate_intent",
            )
            self._advance_cursor(item.cursor)
            return
        if register_result is IntentRegisterResult.IDEMPOTENCY_CONFLICT:
            self._ack(
                intent.intent_id,
                IntentAckStatus.DUPLICATE,
                "duplicate_idempotency_key",
            )
            self._advance_cursor(item.cursor)
            return
        if register_result is IntentRegisterResult.REPLAY:
            record = self._intent_execution_inbox.get(
                execution_identity
            )
            if record is False:
                raise RuntimeError(
                    "durable intent receipt disappeared during replay"
                )
            if record.state is IntentExecutionState.EXCHANGE_CONFIRMED:
                self._state.record_processed(intent)
                self._advance_cursor(item.cursor)
                self._ack(
                    intent.intent_id,
                    IntentAckStatus.DUPLICATE,
                    "durable_exchange_confirmed",
                )
                return
            if record.state is IntentExecutionState.REJECTED:
                self._state.record_processed(intent)
                self._advance_cursor(item.cursor)
                self._ack(
                    intent.intent_id,
                    IntentAckStatus.DUPLICATE,
                    "durable_rejected",
                )
                return

        self._publisher.publish(intent)
        self._state.record_processed(intent)
        self._advance_cursor(item.cursor)
        if canary_identity is False:
            self._ack(
                intent.intent_id,
                IntentAckStatus.ACCEPTED,
                None,
            )
            return
        status, detail = self._live_canary_ack(canary_identity)
        self._ack(intent.intent_id, status, detail)

    def _validate_intent(self, item: IntentItem) -> Optional[ApprovedTradeIntentV1]:
        try:
            payload = _intent_payload(item.intent)
            return ApprovedTradeIntentV1.model_validate(payload)
        except (TypeError, ValueError, ValidationError):
            return None

    def _pending_received_rejection(
        self,
        intent: ApprovedTradeIntentV1,
    ) -> tuple[IntentAckStatus, str] | None:
        if _as_aware(intent.valid_until) <= self._now_aware():
            return IntentAckStatus.EXPIRED, "expired"
        if (
            TradingState(self._trading_state()) is TradingState.HALTED
            and intent.action in self._NEW_POSITION_ACTIONS
        ):
            return IntentAckStatus.REJECTED, "halted"
        canary_denial = self._live_canary_denial(intent)
        if canary_denial is not None:
            return IntentAckStatus.REJECTED, canary_denial
        return None

    def _ack(
        self,
        intent_id: UUID,
        status: IntentAckStatus,
        detail: Optional[str],
    ) -> None:
        self._source.ack_intent(
            account_id=self._account_id,
            node_id=self._node_id,
            intent_id=intent_id,
            status=status,
            detail=detail,
        )

    def _advance_cursor(self, cursor: str) -> None:
        self._state.last_cursor = cursor
        self._offset_store.save(self._state)

    def _now_aware(self) -> datetime:
        return _as_aware(self._now())

    def _live_canary_denial(
        self,
        intent: ApprovedTradeIntentV1,
    ) -> str | None:
        gate_denial = self._live_open_gate_denial(intent)
        if gate_denial is not None:
            return gate_denial
        if not self._live_canary_applies(intent):
            return None
        if intent.action is not IntentAction.OPEN_POSITION:
            return "canary_open_position_only"
        order_plan = intent.order_plan
        permit = order_plan.get("canary_permit")
        if not isinstance(permit, Mapping):
            return "canary_permit_missing"
        identity = _canary_identity(permit)
        if identity is None:
            return "canary_permit_invalid"
        permit_id, release_id = identity
        if str(permit.get("account_id") or "").strip() != self._account_id:
            return "canary_account_mismatch"
        if str(permit.get("node_id") or "").strip() != self._node_id:
            return "canary_node_mismatch"
        if release_id != self._live_canary_release_id:
            return "canary_release_mismatch"
        expires_at = _permit_expiry(permit.get("expires_at"))
        if expires_at is False:
            return "canary_permit_expiry_invalid"
        if expires_at <= self._now_aware():
            return "canary_permit_expired"
        permit_symbol = _canonical_symbol(permit.get("symbol"))
        intent_symbol = _canonical_symbol(intent.instrument_id)
        if not permit_symbol or permit_symbol != intent_symbol:
            return "canary_symbol_mismatch"
        expected_baseline = str(
            permit.get("portfolio_baseline_sha256") or ""
        ).strip()
        if re.fullmatch(r"[0-9a-f]{64}", expected_baseline) is None:
            return "canary_portfolio_baseline_invalid"
        baseline_provider = self._live_canary_portfolio_baseline
        if baseline_provider is None:
            return "canary_portfolio_baseline_unavailable"
        try:
            current_baseline = baseline_provider(permit_symbol)
        except Exception:
            return "canary_portfolio_baseline_unavailable"
        current_baseline = str(current_baseline or "").strip()
        if re.fullmatch(r"[0-9a-f]{64}", current_baseline) is None:
            return "canary_portfolio_baseline_unavailable"
        if current_baseline != expected_baseline:
            return "canary_portfolio_baseline_drift"
        permit_notional = _positive_decimal(
            permit.get("max_notional_usdt")
        )
        permit_loss_limit = _positive_decimal(
            permit.get("max_cumulative_loss_usdt")
        )
        intent_notional = _positive_decimal(
            intent.risk_budget.max_notional
        )
        if (
            permit_notional is None
            or permit_loss_limit is None
            or intent_notional is None
        ):
            return "canary_notional_invalid"
        if permit_notional > Decimal("12"):
            return "canary_notional_exceeded"
        if permit_loss_limit >= Decimal("1.5"):
            return "canary_loss_limit_exceeded"
        if intent_notional > permit_notional:
            return "canary_notional_exceeded"
        if str(order_plan.get("type") or "").strip().lower() != "limit":
            return "canary_limit_ioc_required"
        if (
            str(order_plan.get("time_in_force") or "").strip().upper()
            != "IOC"
        ):
            return "canary_limit_ioc_required"
        quantity = _positive_decimal(order_plan.get("quantity"))
        price = _positive_decimal(order_plan.get("price"))
        if quantity is None or price is None:
            return "canary_limit_quantity_price_required"
        actual_notional = quantity * price
        if actual_notional > permit_notional:
            return "canary_notional_exceeded"
        if actual_notional > Decimal("12"):
            return "canary_notional_exceeded"
        return None

    def _live_open_gate_denial(
        self,
        intent: ApprovedTradeIntentV1,
    ) -> str | None:
        if not self._live_canary_release_id:
            return None
        if not is_live_canary_account(self._account_id):
            return None
        if intent.action not in self._NEW_POSITION_ACTIONS:
            return None
        order_plan = intent.order_plan
        if isinstance(order_plan.get("canary_permit"), Mapping):
            return None
        try:
            trusted_gate = self._live_open_gate()
        except Exception:
            trusted_gate = False
        normalized_trusted = normalize_live_open_gate(trusted_gate)
        if normalized_trusted is False:
            return "live_open_gate_unavailable"
        if normalized_trusted["mode"] == "canary_only":
            return "canary_permit_missing"
        return live_open_gate_denial(
            order_plan.get("live_open_gate"),
            trusted_gate=normalized_trusted,
            expected_release_id=self._live_canary_release_id,
            require_normal=True,
        )

    def _live_canary_applies(
        self,
        intent: ApprovedTradeIntentV1,
    ) -> bool:
        if not self._live_canary_release_id:
            return False
        if not is_live_canary_account(self._account_id):
            return False
        if intent.action not in self._NEW_POSITION_ACTIONS:
            return False
        order_plan = intent.order_plan
        rollout_phase = str(
            order_plan.get("rollout_phase") or ""
        ).strip()
        if not rollout_phase:
            rollout_phase = str(
                self._live_rollout_phase() or ""
            ).strip()
        if live_canary_permit_required(
            self._account_id,
            rollout_phase,
        ):
            return True
        return "canary_permit" in order_plan

    def _live_canary_identity(
        self,
        intent: ApprovedTradeIntentV1,
    ) -> LiveCanaryExecutionIdentity | bool:
        if not self._live_canary_applies(intent):
            return False
        if intent.action is not IntentAction.OPEN_POSITION:
            return False
        permit = intent.order_plan.get("canary_permit")
        if not isinstance(permit, Mapping):
            return False
        identity = _canary_identity(permit)
        if identity is None:
            return False
        permit_id, release_id = identity
        return LiveCanaryExecutionIdentity(
            permit_id=permit_id,
            release_id=release_id,
            intent_id=str(intent.intent_id),
            client_order_id=deterministic_canary_client_order_id(
                intent.intent_id
            ),
            account_id=str(permit.get("account_id") or ""),
            node_id=str(permit.get("node_id") or ""),
            symbol=_canonical_symbol(permit.get("symbol")),
            max_notional_usdt=str(
                permit.get("max_notional_usdt") or ""
            ),
            max_cumulative_loss_usdt=str(
                permit.get("max_cumulative_loss_usdt") or ""
            ),
            authorized_limit_price_usdt=str(
                intent.order_plan.get("price") or ""
            ),
            expires_at=str(permit.get("expires_at") or ""),
            portfolio_baseline_sha256=str(
                permit.get("portfolio_baseline_sha256") or ""
            ),
        ).normalized()

    def _live_canary_ack(
        self,
        identity: LiveCanaryExecutionIdentity,
    ) -> tuple[IntentAckStatus, str]:
        record = self._live_canary_execution_store.get(identity)
        if record is False:
            raise RuntimeError(
                "live canary execution state disappeared after publication"
            )
        if record.state is LiveCanaryExecutionState.RECEIVED:
            return IntentAckStatus.RECEIVED, "canary_received"
        if record.state is LiveCanaryExecutionState.CLAIMED:
            return IntentAckStatus.RECEIVED, "canary_recovery_pending"
        if record.state is LiveCanaryExecutionState.DISPATCHED:
            return IntentAckStatus.ACCEPTED, "canary_dispatched"
        if record.state is LiveCanaryExecutionState.EXCHANGE_CONFIRMED:
            return IntentAckStatus.ACCEPTED, "canary_exchange_confirmed"
        raise RuntimeError(
            f"unsupported live canary execution state {record.state}"
        )


def _intent_payload(raw: object) -> object:
    if isinstance(raw, ApprovedTradeIntentV1):
        return raw.model_dump(mode="json")
    return raw


def _extract_intent_id(item: IntentItem) -> Optional[UUID]:
    raw_intent = item.intent
    if isinstance(raw_intent, ApprovedTradeIntentV1):
        return raw_intent.intent_id
    if isinstance(raw_intent, dict) and "intent_id" in raw_intent:
        try:
            return UUID(str(raw_intent["intent_id"]))
        except (TypeError, ValueError):
            return None
    intent_id = getattr(raw_intent, "intent_id", None)
    if intent_id is not None:
        try:
            return UUID(str(intent_id))
        except (TypeError, ValueError):
            return None
    return None


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _canary_identity(
    permit: Mapping[str, Any],
) -> tuple[str, str] | None:
    raw_permit_id = str(permit.get("permit_id") or "").strip()
    release_id = str(permit.get("release_id") or "").strip()
    try:
        permit_id = str(UUID(raw_permit_id))
    except ValueError:
        return None
    if not release_id:
        return None
    return permit_id, release_id


def _canonical_symbol(value: Any) -> str:
    return str(value or "").strip().upper().split("-")[0].split(".")[0]


def _positive_decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not number.is_finite() or number <= 0:
        return None
    return number


def _intent_execution_identity(
    intent: ApprovedTradeIntentV1,
) -> IntentExecutionIdentity:
    raw_action = getattr(intent.action, "value", intent.action)
    return IntentExecutionIdentity(
        account_id=str(intent.account_id),
        intent_id=str(intent.intent_id),
        idempotency_key=str(intent.idempotency_key),
        instrument_id=str(intent.instrument_id),
        action=str(raw_action),
    )


def _permit_expiry(value: Any) -> datetime | bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False
    return _as_aware(parsed)
