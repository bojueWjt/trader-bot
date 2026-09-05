from __future__ import annotations

import sys
import json
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
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
    expired_dispatched_management,
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


@pytest.mark.parametrize("action", ["cancel_order", "move_stop_loss", "open_position", "add_position"])
@pytest.mark.parametrize("state", list(IntentExecutionState))
@pytest.mark.parametrize("age_hours", [23, 24, 25])
def test_expired_management_requires_dispatched_non_entry_and_24h_grace(
    tmp_path: Path, action: str, state: IntentExecutionState, age_hours: int,
) -> None:
    now = datetime(2026, 9, 4, tzinfo=timezone.utc)
    inbox = JsonIntentExecutionInbox(tmp_path / "intent_execution_inbox.json")
    identity = replace(_identity(), action=action)
    payload = _payload(identity)
    payload["valid_until"] = (now - timedelta(hours=age_hours)).isoformat()
    inbox.register_received(identity, payload)
    record = replace(inbox.get(identity), state=state)
    expected = state is IntentExecutionState.DISPATCHED and action in {"cancel_order", "move_stop_loss"} and age_hours > 24
    assert expired_dispatched_management(record, now) is expected


@pytest.mark.parametrize("valid_until", ["invalid", "", None, 123, "2026-09-03T00:00:00Z", "2026-09-03T08:00:00+08:00", "2026-09-03T00:00:00"])
def test_expired_management_timestamp_parsing(tmp_path: Path, valid_until) -> None:
    inbox = JsonIntentExecutionInbox(tmp_path / "intent_execution_inbox.json")
    identity = replace(_identity(), action="cancel_order")
    payload = _payload(identity)
    payload["valid_until"] = valid_until
    inbox.register_received(identity, payload)
    inbox.begin_dispatch(identity, (_client_order_id(identity.intent_id),))
    expected = isinstance(valid_until, str) and valid_until.startswith("2026-")
    assert expired_dispatched_management(inbox.get(identity), datetime(2026, 9, 4, 1, tzinfo=timezone.utc)) is expected


def test_repair_cli_dry_run_backup_and_non_entry_guard(tmp_path: Path) -> None:
    path = tmp_path / "intent_execution_inbox.json"
    inbox = JsonIntentExecutionInbox(path)
    now = datetime.now(timezone.utc)
    eligible = []
    preserved = []
    for action in ("cancel_order", "move_stop_loss", "open_position", "add_position"):
        for state in IntentExecutionState:
            for expired in (False, True):
                identity = replace(_identity(), action=action)
                payload = _payload(identity)
                valid_until = now + timedelta(hours=1)
                if expired:
                    valid_until = now - timedelta(hours=25)
                payload["valid_until"] = valid_until.isoformat()
                inbox.register_received(identity, payload)
                if state is not IntentExecutionState.RECEIVED:
                    inbox.begin_dispatch(identity, (_client_order_id(identity.intent_id),))
                if state is IntentExecutionState.REJECTED:
                    inbox.mark_rejected(identity, "prior_rejection")
                if state is IntentExecutionState.EXCHANGE_CONFIRMED:
                    inbox.mark_exchange_confirmed(identity)
                if state is IntentExecutionState.DISPATCHED and (
                    action == "cancel_order" or (action == "move_stop_loss" and expired)
                ):
                    eligible.append(identity)
                else:
                    preserved.append(inbox.get(identity))
    mismatch = replace(_identity(), action="cancel_order")
    payload = _payload(mismatch)
    payload["action"] = "open_position"
    inbox.register_received(mismatch, payload)
    inbox.begin_dispatch(mismatch, (_client_order_id(mismatch.intent_id),))
    preserved.append(inbox.get(mismatch))
    before = path.read_bytes()
    command = [sys.executable, str(SERVICE_ROOT / "tools" / "inbox_repair.py"), str(path)]

    preview = subprocess.run(command, capture_output=True, text=True, check=True)
    assert "dry_run=true" in preview.stdout
    assert "action=cancel_order" in preview.stdout
    assert path.read_bytes() == before
    assert list(tmp_path.glob("*.bak-*")) == []

    applied = subprocess.run([*command, "--apply"], capture_output=True, text=True, check=True)
    assert f"rejected={len(eligible)}" in applied.stdout
    backups = list(tmp_path.glob("intent_execution_inbox.json.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == before
    assert json.loads(path.read_bytes())["version"] == 1
    for identity in eligible:
        record = inbox.get(identity)
        assert record.state is IntentExecutionState.REJECTED
        assert record.rejection_reason == "inbox_repair_stale_management"
        assert record.exchange_confirmed_client_order_ids == ()
    for record in preserved:
        assert inbox.get(record.identity()) == record
    assert inbox.repair_dispatched_management(now=now) == ((), False)
    assert len(list(tmp_path.glob("*.bak-*"))) == 1


@pytest.mark.parametrize("failure_point", ["backup", "replace"])
def test_repair_io_failure_preserves_original(tmp_path: Path, failure_point: str) -> None:
    path = tmp_path / "intent_execution_inbox.json"
    inbox = JsonIntentExecutionInbox(path)
    identity = replace(_identity(), action="cancel_order")
    inbox.register_received(identity, _payload(identity))
    inbox.begin_dispatch(identity, (_client_order_id(identity.intent_id),))
    before = path.read_bytes()
    target = "runtime.intent_execution_inbox.os.open"
    if failure_point == "replace":
        target = "runtime.intent_execution_inbox.os.replace"
    with patch(target, side_effect=OSError("disk full")), pytest.raises(OSError, match="disk full"):
        inbox.repair_dispatched_management()
    assert path.read_bytes() == before
    if failure_point == "replace":
        backups = list(tmp_path.glob("*.bak-*"))
        assert len(backups) == 1
        assert backups[0].read_bytes() == before


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
