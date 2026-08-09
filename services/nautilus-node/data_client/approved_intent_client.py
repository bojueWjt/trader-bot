from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Protocol
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
    ) -> None:
        self._account_id = account_id
        self._node_id = node_id
        self._source = source
        self._publisher = publisher
        self._offset_store = offset_store
        self._state = offset_store.load()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._trading_state = trading_state or (lambda: TradingState.HALTED)

    @property
    def state(self) -> IntentOffsetState:
        return self._state

    def poll_once(self, limit: int = 100, wait_ms: int = 0) -> int:
        items = self.fetch_once(limit=limit, wait_ms=wait_ms)
        for item in items:
            self.deliver(item)
        return len(items)

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
            self._ack(intent_id, IntentAckStatus.REJECTED, "schema_mismatch")
            self._advance_cursor(item.cursor)
            return

        if intent.account_id != self._account_id:
            self._ack(intent.intent_id, IntentAckStatus.REJECTED, "wrong_account")
            self._advance_cursor(item.cursor)
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

        self._publisher.publish(intent)
        self._state.record_processed(intent)
        self._advance_cursor(item.cursor)
        self._ack(intent.intent_id, IntentAckStatus.ACCEPTED, None)

    def _validate_intent(self, item: IntentItem) -> Optional[ApprovedTradeIntentV1]:
        try:
            payload = _intent_payload(item.intent)
            return ApprovedTradeIntentV1.model_validate(payload)
        except (TypeError, ValueError, ValidationError):
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


def _intent_payload(raw: object) -> object:
    if isinstance(raw, ApprovedTradeIntentV1):
        return raw.model_dump(mode="json")
    return raw


def _extract_intent_id(item: IntentItem) -> UUID:
    raw_intent = item.intent
    if isinstance(raw_intent, ApprovedTradeIntentV1):
        return raw_intent.intent_id
    if isinstance(raw_intent, dict) and "intent_id" in raw_intent:
        return UUID(str(raw_intent["intent_id"]))
    intent_id = getattr(raw_intent, "intent_id", None)
    if intent_id is not None:
        return UUID(str(intent_id))
    raise ValueError("cannot ack invalid intent without intent_id")


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
