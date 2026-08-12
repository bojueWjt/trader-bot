from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from runtime.intent_execution_inbox import (  # noqa: E402
    IntentDispatchResult,
    IntentExecutionIdentity,
    IntentExecutionInboxError,
    IntentExecutionState,
    IntentRegisterResult,
    JsonIntentExecutionInbox,
)


def test_received_payload_survives_restart_and_duplicate_delivery(
    tmp_path: Path,
) -> None:
    path = tmp_path / "intent-execution-inbox.json"
    identity = _identity()
    payload = {
        "schema_version": "1.0",
        "intent_id": identity.intent_id,
        "account_id": identity.account_id,
        "instrument_id": identity.instrument_id,
        "action": identity.action,
        "idempotency_key": identity.idempotency_key,
    }

    first = JsonIntentExecutionInbox(path)
    assert (
        first.register_received(identity, payload)
        is IntentRegisterResult.REGISTERED
    )

    restarted = JsonIntentExecutionInbox(path)
    assert (
        restarted.register_received(identity, payload)
        is IntentRegisterResult.REPLAY
    )
    records = restarted.pending()
    assert len(records) == 1
    assert records[0].state is IntentExecutionState.RECEIVED
    assert records[0].intent_payload == payload


def test_dispatch_is_durable_before_submit_and_requires_stable_order_id(
    tmp_path: Path,
) -> None:
    inbox = JsonIntentExecutionInbox(
        tmp_path / "intent-execution-inbox.json"
    )
    identity = _identity()
    inbox.register_received(identity, _payload(identity))
    stable_id = _client_order_id(identity.intent_id)

    assert (
        inbox.begin_dispatch(identity, (stable_id,))
        is IntentDispatchResult.READY
    )

    restarted = JsonIntentExecutionInbox(
        tmp_path / "intent-execution-inbox.json"
    )
    assert (
        restarted.begin_dispatch(identity, (stable_id,))
        is IntentDispatchResult.RECOVERY_REQUIRED
    )
    record = restarted.get(identity)
    assert record
    assert record.state is IntentExecutionState.DISPATCHED
    assert record.client_order_ids == (stable_id,)

    with pytest.raises(
        RuntimeError,
        match="client_order_id must be derived from intent_id",
    ):
        restarted.begin_dispatch(identity, ("operator-supplied-id",))


def test_exchange_confirmation_is_a_long_lived_duplicate_barrier(
    tmp_path: Path,
) -> None:
    path = tmp_path / "intent-execution-inbox.json"
    identity = _identity()
    client_order_id = _client_order_id(identity.intent_id)
    inbox = JsonIntentExecutionInbox(path)
    inbox.register_received(identity, _payload(identity))
    inbox.begin_dispatch(identity, (client_order_id,))
    assert inbox.mark_exchange_confirmed_by_client_order_id(
        client_order_id
    )

    restarted = JsonIntentExecutionInbox(path)
    record = restarted.get(identity)
    assert record
    assert record.state is IntentExecutionState.EXCHANGE_CONFIRMED
    assert (
        restarted.begin_dispatch(identity, (client_order_id,))
        is IntentDispatchResult.EXCHANGE_CONFIRMED
    )
    assert restarted.pending() == ()


def test_multi_order_intent_requires_every_exchange_confirmation(
    tmp_path: Path,
) -> None:
    identity = _identity()
    first_order_id = _client_order_id(identity.intent_id, sequence=1)
    second_order_id = _client_order_id(identity.intent_id, sequence=2)
    inbox = JsonIntentExecutionInbox(
        tmp_path / "intent-execution-inbox.json"
    )
    inbox.register_received(identity, _payload(identity))
    inbox.begin_dispatch(
        identity,
        (first_order_id, second_order_id),
    )

    assert inbox.mark_exchange_confirmed_by_client_order_id(
        first_order_id
    )
    record = inbox.get(identity)
    assert record
    assert record.state is IntentExecutionState.DISPATCHED

    assert inbox.mark_exchange_confirmed_by_client_order_id(
        second_order_id
    )
    record = inbox.get(identity)
    assert record
    assert record.state is IntentExecutionState.EXCHANGE_CONFIRMED


def test_new_inbox_rejects_payload_over_max_bytes_before_replace(
    tmp_path: Path,
) -> None:
    path = tmp_path / "intent-execution-inbox.json"
    identity = _identity()
    inbox = JsonIntentExecutionInbox(path, max_bytes=64)

    with pytest.raises(
        IntentExecutionInboxError,
        match=r"payload exceeds max_bytes: \d+ bytes exceeds 64 bytes",
    ):
        inbox.register_received(identity, _payload(identity))

    assert not path.exists()


def test_existing_inbox_remains_unchanged_when_next_write_exceeds_limit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "intent-execution-inbox.json"
    first_identity = _identity()
    JsonIntentExecutionInbox(path).register_received(
        first_identity,
        _payload(first_identity),
    )
    original_payload = path.read_bytes()
    second_identity = _identity()
    inbox = JsonIntentExecutionInbox(
        path,
        max_bytes=len(original_payload) + 32,
    )

    with pytest.raises(
        IntentExecutionInboxError,
        match=r"payload exceeds max_bytes: \d+ bytes exceeds \d+ bytes",
    ):
        inbox.register_received(
            second_identity,
            {
                **_payload(second_identity),
                "padding": "x" * 256,
            },
        )

    assert path.read_bytes() == original_payload
    restarted = JsonIntentExecutionInbox(path)
    assert restarted.get(first_identity)
    assert restarted.get(second_identity) is False


def test_existing_oversized_inbox_fails_closed_without_modification(
    tmp_path: Path,
) -> None:
    path = tmp_path / "intent-execution-inbox.json"
    identity = _identity()
    JsonIntentExecutionInbox(path).register_received(
        identity,
        _payload(identity),
    )
    original_payload = path.read_bytes()
    inbox = JsonIntentExecutionInbox(
        path,
        max_bytes=len(original_payload) - 1,
    )

    with pytest.raises(
        IntentExecutionInboxError,
        match=(
            rf"payload exceeds max_bytes: {len(original_payload)} bytes "
            rf"exceeds {len(original_payload) - 1} bytes"
        ),
    ):
        inbox.pending()

    assert path.read_bytes() == original_payload


@pytest.mark.parametrize("max_bytes", [0, -1, True, 1.5])
def test_max_bytes_must_be_a_positive_integer(
    tmp_path: Path,
    max_bytes: object,
) -> None:
    with pytest.raises(
        ValueError,
        match="max_bytes must be a positive integer",
    ):
        JsonIntentExecutionInbox(
            tmp_path / "intent-execution-inbox.json",
            max_bytes=max_bytes,  # type: ignore[arg-type]
        )


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
