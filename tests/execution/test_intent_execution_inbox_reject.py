"""Inbox operation outcome vs per-leg confirmation facts."""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
for _path in (str(SERVICE_ROOT), str(DOMAIN_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from runtime.intent_execution_inbox import (  # noqa: E402
    IntentExecutionIdentity,
    IntentExecutionState,
    JsonIntentExecutionInbox,
)


def test_pure_deny_stays_rejected_without_overall_confirm(tmp_path: Path) -> None:
    inbox, identity, first, _second = _two_leg_inbox(tmp_path)
    inbox.mark_rejected(identity, "order_denied")

    record = inbox.get(identity)
    assert record.state is IntentExecutionState.REJECTED
    assert record.exchange_confirmed_client_order_ids == ()
    inbox.mark_exchange_confirmed(identity)
    inbox.mark_exchange_confirmed_by_client_order_id(_seq99(identity.intent_id))
    record = inbox.get(identity)
    assert record.state is IntentExecutionState.REJECTED
    assert record.exchange_confirmed_client_order_ids == ()


def test_partial_confirm_then_deny_preserves_leg_receipt(tmp_path: Path) -> None:
    inbox, identity, first, second = _two_leg_inbox(tmp_path)
    assert inbox.mark_exchange_confirmed_by_client_order_id(first)
    record = inbox.get(identity)
    assert record.state is IntentExecutionState.DISPATCHED
    assert record.exchange_confirmed_client_order_ids == (first,)

    inbox.mark_rejected_by_client_order_id(second, "order_denied")
    record = inbox.get(identity)
    assert record.state is IntentExecutionState.REJECTED
    assert record.exchange_confirmed_client_order_ids == (first,)
    assert record.rejection_reason == "order_denied"


def test_rejected_records_late_real_leg_without_promoting(tmp_path: Path) -> None:
    inbox = JsonIntentExecutionInbox(tmp_path / "intent-execution-inbox.json")
    identity = _identity()
    first = _client_order_id(identity.intent_id, sequence=1)
    second = _client_order_id(identity.intent_id, sequence=2)
    third = _client_order_id(identity.intent_id, sequence=3)
    inbox.register_received(identity, _payload(identity))
    inbox.begin_dispatch(identity, (first, second, third))
    inbox.mark_exchange_confirmed_by_client_order_id(first)
    inbox.mark_rejected_by_client_order_id(second, "order_denied")

    restarted = JsonIntentExecutionInbox(
        tmp_path / "intent-execution-inbox.json"
    )
    assert restarted.mark_exchange_confirmed_by_client_order_id(third)
    restarted.mark_exchange_confirmed(identity)
    restarted.mark_exchange_confirmed_by_client_order_id(
        _seq99(identity.intent_id)
    )

    record = restarted.get(identity)
    assert record.state is IntentExecutionState.REJECTED
    assert record.exchange_confirmed_client_order_ids == (first, third)


def test_identity_complete_does_not_copy_all_legs_after_reject(
    tmp_path: Path,
) -> None:
    inbox, identity, first, second = _two_leg_inbox(tmp_path)
    inbox.mark_exchange_confirmed_by_client_order_id(first)
    inbox.mark_rejected(identity, "order_denied")
    inbox.mark_exchange_confirmed(identity)

    record = inbox.get(identity)
    assert record.state is IntentExecutionState.REJECTED
    assert record.exchange_confirmed_client_order_ids == (first,)
    assert second not in record.exchange_confirmed_client_order_ids


def _two_leg_inbox(tmp_path: Path):
    inbox = JsonIntentExecutionInbox(tmp_path / "intent-execution-inbox.json")
    identity = _identity()
    first = _client_order_id(identity.intent_id, sequence=1)
    second = _client_order_id(identity.intent_id, sequence=2)
    inbox.register_received(identity, _payload(identity))
    inbox.begin_dispatch(identity, (first, second))
    return inbox, identity, first, second


def _identity() -> IntentExecutionIdentity:
    intent_id = str(uuid4())
    return IntentExecutionIdentity(
        account_id="account-b",
        intent_id=intent_id,
        idempotency_key=f"idempotency-{intent_id}",
        instrument_id="SOLUSDT-PERP.BINANCE",
        action="open_position",
    )


def _payload(identity: IntentExecutionIdentity) -> dict[str, str]:
    return {
        "schema_version": "1.0",
        "intent_id": identity.intent_id,
        "account_id": identity.account_id,
        "instrument_id": identity.instrument_id,
        "action": identity.action,
        "idempotency_key": identity.idempotency_key,
    }


def _client_order_id(intent_id: str, *, sequence: int = 1) -> str:
    return f"B{intent_id.replace('-', '')}{sequence:02d}"


def _seq99(intent_id: str) -> str:
    return _client_order_id(intent_id, sequence=99)
