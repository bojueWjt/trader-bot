from __future__ import annotations

import sys
from hashlib import sha256
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
ADAPTER_ROOT = REPO_ROOT / "packages" / "nautilus-adapter"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))
sys.path.insert(0, str(ADAPTER_ROOT))

from data_client.approved_intent_client import (  # noqa: E402
    ApprovedIntentDataClient,
    IntentOffsetState,
    JsonIntentOffsetStore,
)
import data_client.approved_intent_client as intent_client_module  # noqa: E402
from execution_domain.contracts import ApprovedTradeIntentV1, IntentAction, RiskBudget  # noqa: E402
from execution_domain.control_plane import (  # noqa: E402
    IntentAckStatus,
    IntentItem,
    TradingState,
)
from execution_domain.testing import InMemoryControlPlane  # noqa: E402
from runtime.live_canary_execution import (  # noqa: E402
    JsonLiveCanaryExecutionStore,
    LiveCanaryClaimResult,
    LiveCanaryExecutionIdentity,
    deterministic_canary_client_order_id,
)
from runtime.intent_execution_inbox import (  # noqa: E402
    IntentExecutionIdentity,
    IntentExecutionState,
    JsonIntentExecutionInbox,
)


ACCOUNT_ID = "account-a"
NODE_ID = "node-a"
NOW = datetime(2026, 6, 19, 12, tzinfo=timezone.utc)


def test_offset_save_fsyncs_file_and_parent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fsync_calls: list[int] = []
    original_fsync = intent_client_module.os.fsync

    def tracking_fsync(file_descriptor: int) -> None:
        fsync_calls.append(file_descriptor)
        original_fsync(file_descriptor)

    monkeypatch.setattr(
        intent_client_module.os,
        "fsync",
        tracking_fsync,
    )
    store = JsonIntentOffsetStore(tmp_path / "intent-offset.json")

    store.save(IntentOffsetState(last_cursor="cursor-1"))

    assert len(fsync_calls) == 2
    assert store.load().last_cursor == "cursor-1"


def test_schema_mismatch_rejects_without_publish(tmp_path: Path) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    intent = _intent(schema_version="9.9")  # type: ignore[arg-type]
    control_plane.add_intent(ACCOUNT_ID, IntentItem(cursor="c1", intent=intent))
    publisher = _RecordingPublisher()

    client = _client(tmp_path, control_plane, publisher)

    assert client.poll_once() == 1
    assert publisher.published == []
    assert control_plane.intent_acks == [
        (ACCOUNT_ID, NODE_ID, intent.intent_id, IntentAckStatus.REJECTED, "schema_mismatch")
    ]


def test_expired_intent_acks_expired_without_publish(tmp_path: Path) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    intent = _intent(valid_until=NOW - timedelta(seconds=1))
    control_plane.add_intent(ACCOUNT_ID, IntentItem(cursor="c1", intent=intent))
    publisher = _RecordingPublisher()

    client = _client(tmp_path, control_plane, publisher)

    assert client.poll_once() == 1
    assert publisher.published == []
    assert control_plane.intent_acks == [
        (ACCOUNT_ID, NODE_ID, intent.intent_id, IntentAckStatus.EXPIRED, "expired")
    ]


def test_wrong_account_rejects_without_publish(tmp_path: Path) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    intent = _intent(account_id="account-b")
    control_plane.add_intent(ACCOUNT_ID, IntentItem(cursor="c1", intent=intent))
    publisher = _RecordingPublisher()

    client = _client(tmp_path, control_plane, publisher)

    assert client.poll_once() == 1
    assert publisher.published == []
    assert control_plane.intent_acks == [
        (ACCOUNT_ID, NODE_ID, intent.intent_id, IntentAckStatus.REJECTED, "wrong_account")
    ]


def test_halted_node_rejects_new_position_intent(tmp_path: Path) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    intent = _intent(action=IntentAction.OPEN_POSITION)
    control_plane.add_intent(ACCOUNT_ID, IntentItem(cursor="c1", intent=intent))
    publisher = _RecordingPublisher()

    client = _client(
        tmp_path,
        control_plane,
        publisher,
        trading_state=lambda: TradingState.HALTED,
    )

    assert client.poll_once() == 1
    assert publisher.published == []
    assert control_plane.intent_acks == [
        (ACCOUNT_ID, NODE_ID, intent.intent_id, IntentAckStatus.REJECTED, "halted")
    ]


def test_live_account_a_canary_is_single_use_per_release(
    tmp_path: Path,
) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    first = _live_canary_intent()
    second = _live_canary_intent()
    control_plane.add_intent(
        ACCOUNT_ID,
        IntentItem(cursor="c1", intent=first),
    )
    control_plane.add_intent(
        ACCOUNT_ID,
        IntentItem(cursor="c2", intent=second),
    )
    publisher = _RecordingPublisher()
    client = _client(
        tmp_path,
        control_plane,
        publisher,
        live_canary_release_id="release-a",
    )

    assert client.poll_once(limit=10) == 2

    assert [intent.intent_id for intent in publisher.published] == [
        first.intent_id
    ]
    assert control_plane.intent_acks == [
        (
            ACCOUNT_ID,
            NODE_ID,
            first.intent_id,
            IntentAckStatus.RECEIVED,
            "canary_received",
        ),
        (
            ACCOUNT_ID,
            NODE_ID,
            second.intent_id,
            IntentAckStatus.REJECTED,
            "canary_permit_already_claimed",
        ),
    ]


@pytest.mark.parametrize(
    ("account_id", "node_id"),
    (
        ("account-b", "node-b"),
        ("account-c", "node-c"),
        ("account-d", "node-d"),
    ),
)
def test_live_secondary_accounts_require_canary_permit_for_open_intents(
    tmp_path: Path,
    account_id: str,
    node_id: str,
) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    intent = _intent(
        account_id=account_id,
        action=IntentAction.OPEN_POSITION,
            order_plan={
                "type": "market",
                "side": "buy",
                "quantity": "0.001",
            },
    )
    control_plane.add_intent(
        account_id,
        IntentItem(cursor="c1", intent=intent),
    )
    publisher = _RecordingPublisher()
    client = _client(
        tmp_path,
        control_plane,
        publisher,
        account_id=account_id,
        node_id=node_id,
        live_canary_release_id="release-a",
        live_open_gate=_canary_live_open_gate(),
    )

    assert client.poll_once() == 1
    assert publisher.published == []
    assert control_plane.intent_acks == [
        (
            account_id,
            node_id,
            intent.intent_id,
            IntentAckStatus.REJECTED,
            "canary_permit_missing",
        )
    ]


@pytest.mark.parametrize(
    ("account_id", "node_id"),
    (
        ("account-a", "node-a"),
        ("account-b", "node-b"),
        ("account-c", "node-c"),
        ("account-d", "node-d"),
    ),
)
def test_fleet_complete_regular_open_skips_canary_permit(
    tmp_path: Path,
    account_id: str,
    node_id: str,
) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    intent = _intent(
        account_id=account_id,
        action=IntentAction.OPEN_POSITION,
        order_plan={
            "type": "market",
            "side": "buy",
            "quantity": "0.001",
            "rollout_phase": "fleet_complete",
            "live_open_gate": _normal_live_open_gate(),
        },
    )
    control_plane.add_intent(
        account_id,
        IntentItem(cursor="c1", intent=intent),
    )
    publisher = _RecordingPublisher()
    client = _client(
        tmp_path,
        control_plane,
        publisher,
        account_id=account_id,
        node_id=node_id,
        live_canary_release_id="release-a",
        live_open_gate=_normal_live_open_gate(),
    )

    assert client.poll_once() == 1
    assert publisher.published == [intent]
    assert control_plane.intent_acks == [
        (
            account_id,
            node_id,
            intent.intent_id,
            IntentAckStatus.ACCEPTED,
            None,
        )
    ]


def test_fleet_complete_rejects_legacy_rollout_phase_without_gate(
    tmp_path: Path,
) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    intent = _intent(
        action=IntentAction.OPEN_POSITION,
        order_plan={
            "type": "market",
            "side": "buy",
            "quantity": "0.001",
            "rollout_phase": "fleet_complete",
        },
    )
    control_plane.add_intent(
        ACCOUNT_ID,
        IntentItem(cursor="c1", intent=intent),
    )
    publisher = _RecordingPublisher()
    client = _client(
        tmp_path,
        control_plane,
        publisher,
        live_canary_release_id="release-a",
        live_open_gate=_normal_live_open_gate(),
    )

    assert client.poll_once() == 1
    assert publisher.published == []
    assert control_plane.intent_acks[-1][4] == "live_open_gate_missing"


def test_fleet_complete_rejects_live_open_gate_release_mismatch(
    tmp_path: Path,
) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    mismatched_gate = _normal_live_open_gate()
    mismatched_gate["release_id"] = "release-b"
    intent = _intent(
        action=IntentAction.OPEN_POSITION,
        order_plan={
            "type": "market",
            "side": "buy",
            "quantity": "0.001",
            "live_open_gate": mismatched_gate,
        },
    )
    control_plane.add_intent(
        ACCOUNT_ID,
        IntentItem(cursor="c1", intent=intent),
    )
    publisher = _RecordingPublisher()
    client = _client(
        tmp_path,
        control_plane,
        publisher,
        live_canary_release_id="release-a",
        live_open_gate=_normal_live_open_gate(),
    )

    assert client.poll_once() == 1
    assert publisher.published == []
    assert control_plane.intent_acks[-1][4] == (
        "live_open_gate_release_mismatch"
    )


@pytest.mark.parametrize(
    ("account_id", "node_id"),
    (
        ("account-b", "node-b"),
        ("account-c", "node-c"),
        ("account-d", "node-d"),
    ),
)
def test_live_secondary_accounts_accept_explicit_canary_and_persist(
    tmp_path: Path,
    account_id: str,
    node_id: str,
) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    intent = _live_canary_intent(
        account_id=account_id,
        order_plan=_canary_order_plan(
            account_id=account_id,
            node_id=node_id,
        ),
    )
    control_plane.add_intent(
        account_id,
        IntentItem(cursor="c1", intent=intent),
    )
    publisher = _RecordingPublisher()
    client = _client(
        tmp_path,
        control_plane,
        publisher,
        account_id=account_id,
        node_id=node_id,
        live_canary_release_id="release-a",
    )

    assert client.poll_once() == 1
    assert publisher.published == [intent]
    assert control_plane.intent_acks[-1] == (
        account_id,
        node_id,
        intent.intent_id,
        IntentAckStatus.RECEIVED,
        "canary_received",
    )
    record = JsonLiveCanaryExecutionStore(
        tmp_path / "live-canary-execution.json"
    ).get(_execution_identity(intent))
    assert record
    assert record.account_id == account_id
    assert record.node_id == node_id


@pytest.mark.parametrize(
    ("account_id", "node_id"),
    (
        ("account-b", "node-b"),
        ("account-c", "node-c"),
        ("account-d", "node-d"),
    ),
)
def test_live_secondary_canary_rejects_cross_account_permit(
    tmp_path: Path,
    account_id: str,
    node_id: str,
) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    intent = _live_canary_intent(
        account_id=account_id,
        order_plan=_canary_order_plan(
            account_id="account-a",
            node_id=node_id,
        ),
    )
    control_plane.add_intent(
        account_id,
        IntentItem(cursor="c1", intent=intent),
    )
    publisher = _RecordingPublisher()
    client = _client(
        tmp_path,
        control_plane,
        publisher,
        account_id=account_id,
        node_id=node_id,
        live_canary_release_id="release-a",
    )

    assert client.poll_once() == 1
    assert publisher.published == []
    assert control_plane.intent_acks[-1][4] == "canary_account_mismatch"


def test_live_account_a_canary_rejects_wrong_identity_and_over_cap(
    tmp_path: Path,
) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    wrong_node = _live_canary_intent(
        order_plan=_canary_order_plan(
            node_id="node-b",
        )
    )
    over_cap = _live_canary_intent(
        order_plan=_canary_order_plan(max_notional="12.01"),
        risk_budget=RiskBudget(
            risk_fraction=0.01,
            max_notional=12.01,
            max_leverage=2.0,
        ),
    )
    control_plane.add_intent(
        ACCOUNT_ID,
        IntentItem(cursor="c1", intent=wrong_node),
    )
    control_plane.add_intent(
        ACCOUNT_ID,
        IntentItem(cursor="c2", intent=over_cap),
    )
    publisher = _RecordingPublisher()
    client = _client(
        tmp_path,
        control_plane,
        publisher,
        live_canary_release_id="release-a",
    )

    assert client.poll_once(limit=10) == 2

    assert publisher.published == []
    assert [ack[4] for ack in control_plane.intent_acks] == [
        "canary_node_mismatch",
        "canary_notional_exceeded",
    ]


def test_live_canary_rejects_expired_permit_and_baseline_drift(
    tmp_path: Path,
) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    expired = _live_canary_intent(
        order_plan=_canary_order_plan(
            expires_at=NOW - timedelta(seconds=1),
        )
    )
    drifted = _live_canary_intent()
    control_plane.add_intent(
        ACCOUNT_ID,
        IntentItem(cursor="c1", intent=expired),
    )
    control_plane.add_intent(
        ACCOUNT_ID,
        IntentItem(cursor="c2", intent=drifted),
    )
    publisher = _RecordingPublisher()
    client = _client(
        tmp_path,
        control_plane,
        publisher,
        live_canary_release_id="release-a",
        portfolio_baseline="5" * 64,
    )

    assert client.poll_once(limit=10) == 2

    assert publisher.published == []
    assert [ack[4] for ack in control_plane.intent_acks] == [
        "canary_permit_expired",
        "canary_portfolio_baseline_drift",
    ]


def test_live_canary_requires_local_portfolio_baseline_provider(
    tmp_path: Path,
) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    intent = _live_canary_intent()
    control_plane.add_intent(
        ACCOUNT_ID,
        IntentItem(cursor="c1", intent=intent),
    )
    publisher = _RecordingPublisher()
    client = _client(
        tmp_path,
        control_plane,
        publisher,
        live_canary_release_id="release-a",
        portfolio_baseline=None,
    )

    assert client.poll_once() == 1
    assert publisher.published == []
    assert control_plane.intent_acks[-1][4] == (
        "canary_portfolio_baseline_unavailable"
    )


def test_live_canary_publication_failure_replays_without_rejection(
    tmp_path: Path,
) -> None:
    intent = _live_canary_intent()
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    control_plane.add_intent(
        ACCOUNT_ID,
        IntentItem(cursor="c1", intent=intent),
    )
    failing = _FailingPublisher()
    first = _client(
        tmp_path,
        control_plane,
        failing,
        live_canary_release_id="release-a",
    )

    with pytest.raises(RuntimeError, match="publication failed"):
        first.poll_once()

    restarted_publisher = _RecordingPublisher()
    restarted = _client(
        tmp_path,
        control_plane,
        restarted_publisher,
        live_canary_release_id="release-a",
    )

    assert restarted.poll_once() == 1
    assert restarted_publisher.published == [intent]
    assert control_plane.intent_acks[-1] == (
        ACCOUNT_ID,
        NODE_ID,
        intent.intent_id,
        IntentAckStatus.RECEIVED,
        "canary_received",
    )


def test_submit_crash_before_cursor_and_ack_recovers_without_rejection(
    tmp_path: Path,
) -> None:
    intent = _live_canary_intent()
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    control_plane.add_intent(
        ACCOUNT_ID,
        IntentItem(cursor="c1", intent=intent),
    )
    execution_path = tmp_path / "live-canary-execution.json"
    submitted_order_ids: list[str] = []
    first_publisher = _CrashAfterSubmitPublisher(
        execution_path,
        submitted_order_ids,
    )
    first = _client(
        tmp_path,
        control_plane,
        first_publisher,
        live_canary_release_id="release-a",
    )

    with pytest.raises(SystemExit, match="crash before cursor and ack"):
        first.poll_once()

    restarted_publisher = _RecoveryPublisher(
        execution_path,
        existing_order_ids=set(submitted_order_ids),
    )
    restarted = _client(
        tmp_path,
        control_plane,
        restarted_publisher,
        live_canary_release_id="release-a",
    )

    assert restarted.poll_once() == 1
    assert submitted_order_ids == [
        deterministic_canary_client_order_id(intent.intent_id)
    ]
    assert restarted_publisher.submitted_order_ids == []
    assert control_plane.intent_acks == [
        (
            ACCOUNT_ID,
            NODE_ID,
            intent.intent_id,
            IntentAckStatus.ACCEPTED,
            "canary_exchange_confirmed",
        )
    ]


def test_generic_publication_failure_leaves_received_for_restart(
    tmp_path: Path,
) -> None:
    intent = _intent(action=IntentAction.OPEN_POSITION)
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    control_plane.add_intent(
        ACCOUNT_ID,
        IntentItem(cursor="c1", intent=intent),
    )
    first = _client(tmp_path, control_plane, _FailingPublisher())

    with pytest.raises(RuntimeError, match="publication failed"):
        first.poll_once()

    inbox = JsonIntentExecutionInbox(
        tmp_path / "intent-execution-inbox.json"
    )
    record = inbox.get(_generic_identity(intent))
    assert record
    assert record.state is IntentExecutionState.RECEIVED

    publisher = _RecordingPublisher()
    restarted = _client(tmp_path, control_plane, publisher)
    assert restarted.poll_once() == 1
    assert publisher.published == [intent]


def test_startup_replays_received_after_cursor_was_already_saved(
    tmp_path: Path,
) -> None:
    intent = _intent(action=IntentAction.OPEN_POSITION)
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    control_plane.add_intent(
        ACCOUNT_ID,
        IntentItem(cursor="c1", intent=intent),
    )
    offset_store = JsonIntentOffsetStore(
        tmp_path / "intent-offset.json"
    )
    offset_state = offset_store.load()
    offset_state.last_cursor = "c1"
    offset_state.record_processed(intent)
    offset_store.save(offset_state)
    inbox = JsonIntentExecutionInbox(
        tmp_path / "intent-execution-inbox.json"
    )
    inbox.register_received(
        _generic_identity(intent),
        intent.model_dump(mode="json"),
    )
    publisher = _RecordingPublisher()
    restarted = _client(tmp_path, control_plane, publisher)

    assert restarted.poll_once() == 1
    assert publisher.published == [intent]
    assert control_plane.intent_acks[-1] == (
        ACCOUNT_ID,
        NODE_ID,
        intent.intent_id,
        IntentAckStatus.ACCEPTED,
        "durable_replay",
    )


def test_confirmed_generic_intent_survives_lost_offset_and_closed_position(
    tmp_path: Path,
) -> None:
    intent = _intent(action=IntentAction.OPEN_POSITION)
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    control_plane.add_intent(
        ACCOUNT_ID,
        IntentItem(cursor="c1", intent=intent),
    )
    publisher = _ConfirmingGenericPublisher(
        tmp_path / "intent-execution-inbox.json"
    )
    first = _client(tmp_path, control_plane, publisher)

    assert first.poll_once() == 1
    assert publisher.published == [intent]

    (tmp_path / "intent-offset.json").unlink()
    restarted_publisher = _RecordingPublisher()
    restarted = _client(
        tmp_path,
        control_plane,
        restarted_publisher,
    )

    assert restarted.poll_once() == 1
    assert restarted_publisher.published == []
    assert control_plane.intent_acks[-1] == (
        ACCOUNT_ID,
        NODE_ID,
        intent.intent_id,
        IntentAckStatus.DUPLICATE,
        "durable_exchange_confirmed",
    )


def test_confirmed_canary_replay_precedes_halt_and_permit_expiry(
    tmp_path: Path,
) -> None:
    intent = _live_canary_intent(
        order_plan=_canary_order_plan(
            expires_at=NOW - timedelta(seconds=1),
        )
    )
    identity = _generic_identity(intent)
    inbox = JsonIntentExecutionInbox(
        tmp_path / "intent-execution-inbox.json"
    )
    client_order_id = deterministic_canary_client_order_id(
        intent.intent_id
    )
    inbox.register_received(identity, intent.model_dump(mode="json"))
    inbox.begin_dispatch(identity, (client_order_id,))
    inbox.mark_exchange_confirmed_by_client_order_id(client_order_id)
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    control_plane.add_intent(
        ACCOUNT_ID,
        IntentItem(cursor="c1", intent=intent),
    )
    publisher = _RecordingPublisher()
    client = _client(
        tmp_path,
        control_plane,
        publisher,
        trading_state=lambda: TradingState.HALTED,
        live_canary_release_id="release-a",
        portfolio_baseline="5" * 64,
    )

    assert client.poll_once() == 1
    assert publisher.published == []
    assert control_plane.intent_acks[-1] == (
        ACCOUNT_ID,
        NODE_ID,
        intent.intent_id,
        IntentAckStatus.DUPLICATE,
        "durable_exchange_confirmed",
    )


def test_duplicate_intent_id_or_idempotency_key_does_not_publish_twice(
    tmp_path: Path,
) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    first = _intent()
    same_intent_id = _intent(intent_id=first.intent_id)
    same_key = _intent(idempotency_key=first.idempotency_key)
    control_plane.add_intent(ACCOUNT_ID, IntentItem(cursor="c1", intent=first))
    control_plane.add_intent(ACCOUNT_ID, IntentItem(cursor="c2", intent=same_intent_id))
    control_plane.add_intent(ACCOUNT_ID, IntentItem(cursor="c3", intent=same_key))
    publisher = _RecordingPublisher()

    client = _client(tmp_path, control_plane, publisher)

    assert client.poll_once(limit=10) == 3
    assert [intent.intent_id for intent in publisher.published] == [first.intent_id]
    assert [ack[3] for ack in control_plane.intent_acks] == [
        IntentAckStatus.ACCEPTED,
        IntentAckStatus.DUPLICATE,
        IntentAckStatus.DUPLICATE,
    ]
    assert control_plane.intent_acks[1][4] == "duplicate_intent"
    assert control_plane.intent_acks[2][4] == "duplicate_idempotency_key"


def test_restart_resumes_after_safe_cursor_without_republishing(tmp_path: Path) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    first = _intent()
    second = _intent()
    control_plane.add_intent(ACCOUNT_ID, IntentItem(cursor="c1", intent=first))
    control_plane.add_intent(ACCOUNT_ID, IntentItem(cursor="c2", intent=second))
    publisher = _ConfirmingGenericPublisher(
        tmp_path / "intent-execution-inbox.json"
    )

    first_client = _client(tmp_path, control_plane, publisher)
    assert first_client.poll_once(limit=1) == 1

    restarted_client = _client(tmp_path, control_plane, publisher)
    assert restarted_client.poll_once(limit=10) == 1

    assert [intent.intent_id for intent in publisher.published] == [
        first.intent_id,
        second.intent_id,
    ]
    assert [ack[2] for ack in control_plane.intent_acks] == [
        first.intent_id,
        second.intent_id,
    ]


def test_malformed_item_does_not_block_later_valid_intent(tmp_path: Path) -> None:
    malformed_id = uuid4()
    valid = _intent()
    source = _RawBatchControlPlane(
        items=[
            IntentItem(
                cursor="c1",
                intent={
                    "schema_version": "9.9",
                    "intent_id": str(malformed_id),
                },
            ),
            IntentItem(cursor="c2", intent=valid),
        ]
    )
    publisher = _RecordingPublisher()
    client = ApprovedIntentDataClient(
        account_id=ACCOUNT_ID,
        node_id=NODE_ID,
        source=source,
        publisher=publisher,
        offset_store=JsonIntentOffsetStore(tmp_path / "intent-offset.json"),
        now=lambda: NOW,
        trading_state=lambda: TradingState.ACTIVE,
    )

    assert client.poll_once() == 2
    assert [item.intent_id for item in publisher.published] == [valid.intent_id]
    assert source.intent_acks == [
        (malformed_id, IntentAckStatus.REJECTED, "schema_mismatch"),
        (valid.intent_id, IntentAckStatus.ACCEPTED, None),
    ]
    assert client.state.last_cursor == "c2"


def test_malformed_item_without_valid_id_still_advances_cursor(
    tmp_path: Path,
) -> None:
    valid = _intent()
    source = _RawBatchControlPlane(
        items=[
            IntentItem(
                cursor="c1",
                intent={
                    "schema_version": "9.9",
                    "intent_id": "invalid",
                },
            ),
            IntentItem(cursor="c2", intent=valid),
        ]
    )
    publisher = _RecordingPublisher()
    client = ApprovedIntentDataClient(
        account_id=ACCOUNT_ID,
        node_id=NODE_ID,
        source=source,
        publisher=publisher,
        offset_store=JsonIntentOffsetStore(tmp_path / "intent-offset.json"),
        now=lambda: NOW,
        trading_state=lambda: TradingState.ACTIVE,
    )

    assert client.poll_once() == 2
    assert [item.intent_id for item in publisher.published] == [valid.intent_id]
    assert source.intent_acks == [
        (valid.intent_id, IntentAckStatus.ACCEPTED, None),
    ]
    assert client.state.last_cursor == "c2"


def test_fetch_and_delivery_can_run_as_separate_phases(tmp_path: Path) -> None:
    control_plane = InMemoryControlPlane(now=lambda: NOW)
    intent = _intent()
    control_plane.add_intent(
        ACCOUNT_ID,
        IntentItem(cursor="c1", intent=intent),
    )
    publisher = _RecordingPublisher()
    client = _client(tmp_path, control_plane, publisher)

    items = client.fetch_once(limit=1, wait_ms=0)

    assert len(items) == 1
    assert publisher.published == []
    assert control_plane.intent_acks == []
    assert client.state.last_cursor is None

    client.deliver(items[0])

    assert publisher.published == [intent]
    assert control_plane.intent_acks == [
        (
            ACCOUNT_ID,
            NODE_ID,
            intent.intent_id,
            IntentAckStatus.ACCEPTED,
            None,
        )
    ]
    assert client.state.last_cursor == "c1"


def _client(
    tmp_path: Path,
    control_plane: InMemoryControlPlane,
    publisher: Any,
    trading_state=lambda: TradingState.ACTIVE,
    live_canary_release_id: str | None = None,
    portfolio_baseline: str | None = "4" * 64,
    account_id: str = ACCOUNT_ID,
    node_id: str = NODE_ID,
    live_open_gate: dict[str, Any] | bool = False,
) -> ApprovedIntentDataClient:
    baseline_provider = None
    if portfolio_baseline is not None:
        baseline_provider = lambda symbol: portfolio_baseline
    return ApprovedIntentDataClient(
        account_id=account_id,
        node_id=node_id,
        source=control_plane,
        publisher=publisher,
        offset_store=JsonIntentOffsetStore(tmp_path / "intent-offset.json"),
        now=lambda: NOW,
        trading_state=trading_state,
        live_canary_release_id=live_canary_release_id,
        live_canary_execution_path=(
            tmp_path / "live-canary-execution.json"
        ),
        intent_execution_inbox_path=(
            tmp_path / "intent-execution-inbox.json"
        ),
        live_canary_portfolio_baseline=baseline_provider,
        live_open_gate=lambda: live_open_gate,
    )


def _intent(**overrides: Any) -> ApprovedTradeIntentV1:
    intent_id = overrides.get("intent_id", uuid4())
    values: dict[str, Any] = {
        "schema_version": "1.0",
        "intent_id": intent_id,
        "decision_id": uuid4(),
        "risk_decision_id": uuid4(),
        "account_id": ACCOUNT_ID,
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "action": IntentAction.ADD_POSITION,
        "order_plan": {"type": "market", "side": "buy", "quantity": "0.001"},
        "risk_budget": RiskBudget(
            risk_fraction=0.01,
            max_notional=100.0,
            max_leverage=2.0,
        ),
        "target_position_id": None,
        "valid_until": NOW + timedelta(minutes=5),
        "idempotency_key": sha256(str(intent_id).encode("ascii")).hexdigest(),
        "approved_at": NOW - timedelta(seconds=5),
    }
    values.update(overrides)
    return ApprovedTradeIntentV1.model_construct(**values)


def _live_canary_intent(**overrides: Any) -> ApprovedTradeIntentV1:
    values: dict[str, Any] = {
        "action": IntentAction.OPEN_POSITION,
        "order_plan": _canary_order_plan(),
        "risk_budget": RiskBudget(
            risk_fraction=0.01,
            max_notional=12.0,
            max_leverage=2.0,
        ),
    }
    values.update(overrides)
    return _intent(**values)


def _canary_order_plan(
    *,
    account_id: str = ACCOUNT_ID,
    node_id: str = NODE_ID,
    release_id: str = "release-a",
    max_notional: str = "12",
    expires_at: datetime | None = None,
) -> dict[str, Any]:
    if expires_at is None:
        expires_at = NOW + timedelta(minutes=5)
    return {
        "type": "limit",
        "side": "buy",
        "quantity": "0.1",
        "price": "100",
        "time_in_force": "IOC",
        "canary_permit": {
            "permit_id": str(uuid4()),
            "account_id": account_id,
            "symbol": "BTCUSDT",
            "release_id": release_id,
            "node_id": node_id,
            "max_notional_usdt": max_notional,
            "max_cumulative_loss_usdt": "1.49",
            "expires_at": expires_at.isoformat(),
            "portfolio_baseline_sha256": "4" * 64,
        },
    }


def _canary_live_open_gate() -> dict[str, Any]:
    return {
        "mode": "canary_only",
        "release_id": "release-a",
        "rollout_phase": "account_a_canary",
        "phase_version": 1,
    }


def _normal_live_open_gate() -> dict[str, Any]:
    return {
        "mode": "normal",
        "release_id": "release-a",
        "rollout_phase": "fleet_complete",
        "phase_version": 5,
    }


class _RecordingPublisher:
    def __init__(self) -> None:
        self.published: list[ApprovedTradeIntentV1] = []

    def publish(self, intent: ApprovedTradeIntentV1) -> None:
        self.published.append(intent)


class _FailingPublisher:
    def publish(self, intent: ApprovedTradeIntentV1) -> None:
        del intent
        raise RuntimeError("publication failed")


class _ConfirmingGenericPublisher:
    def __init__(self, inbox_path: Path) -> None:
        self._inbox = JsonIntentExecutionInbox(inbox_path)
        self.published: list[ApprovedTradeIntentV1] = []

    def publish(self, intent: ApprovedTradeIntentV1) -> None:
        self.published.append(intent)
        identity = _generic_identity(intent)
        client_order_id = deterministic_canary_client_order_id(
            intent.intent_id
        )
        self._inbox.begin_dispatch(identity, (client_order_id,))
        self._inbox.mark_exchange_confirmed_by_client_order_id(
            client_order_id
        )


def _generic_identity(
    intent: ApprovedTradeIntentV1,
) -> IntentExecutionIdentity:
    return IntentExecutionIdentity(
        account_id=intent.account_id,
        intent_id=str(intent.intent_id),
        idempotency_key=intent.idempotency_key,
        instrument_id=intent.instrument_id,
        action=str(intent.action.value),
    )


class _CrashAfterSubmitPublisher:
    def __init__(
        self,
        execution_path: Path,
        submitted_order_ids: list[str],
    ) -> None:
        self._store = JsonLiveCanaryExecutionStore(execution_path)
        self._submitted_order_ids = submitted_order_ids

    def publish(self, intent: ApprovedTradeIntentV1) -> None:
        identity = _execution_identity(intent)
        claim = self._store.claim(identity)
        assert claim is LiveCanaryClaimResult.ACQUIRED
        self._submitted_order_ids.append(identity.client_order_id)
        raise SystemExit("crash before cursor and ack")


class _RecoveryPublisher:
    def __init__(
        self,
        execution_path: Path,
        *,
        existing_order_ids: set[str],
    ) -> None:
        self._store = JsonLiveCanaryExecutionStore(execution_path)
        self._existing_order_ids = existing_order_ids
        self.submitted_order_ids: list[str] = []

    def publish(self, intent: ApprovedTradeIntentV1) -> None:
        identity = _execution_identity(intent)
        claim = self._store.claim(identity)
        assert claim is LiveCanaryClaimResult.RECOVERY_REQUIRED
        assert identity.client_order_id in self._existing_order_ids
        self._store.mark_exchange_confirmed(identity)


def _execution_identity(
    intent: ApprovedTradeIntentV1,
) -> LiveCanaryExecutionIdentity:
    permit = intent.order_plan["canary_permit"]
    return LiveCanaryExecutionIdentity(
        permit_id=str(permit["permit_id"]),
        release_id=str(permit["release_id"]),
        intent_id=str(intent.intent_id),
        client_order_id=deterministic_canary_client_order_id(
            intent.intent_id
        ),
        account_id=str(permit["account_id"]),
        node_id=str(permit["node_id"]),
        symbol=str(permit["symbol"]),
        max_notional_usdt=str(permit["max_notional_usdt"]),
        max_cumulative_loss_usdt=str(
            permit["max_cumulative_loss_usdt"]
        ),
        authorized_limit_price_usdt=str(intent.order_plan["price"]),
        expires_at=str(permit["expires_at"]),
        portfolio_baseline_sha256=str(
            permit["portfolio_baseline_sha256"]
        ),
    )


class _RawBatchControlPlane:
    def __init__(self, items: list[IntentItem]) -> None:
        self._items = items
        self.intent_acks: list[tuple[Any, IntentAckStatus, str | None]] = []

    def fetch_intents(
        self,
        account_id: str,
        after_cursor: str | None,
        limit: int,
        wait_ms: int = 0,
    ):
        del account_id, after_cursor, limit, wait_ms
        from execution_domain.control_plane import IntentBatch

        return IntentBatch(items=self._items, next_cursor="c2")

    def ack_intent(
        self,
        account_id: str,
        node_id: str,
        intent_id: Any,
        status: IntentAckStatus,
        detail: str | None = None,
    ) -> None:
        del account_id, node_id
        self.intent_acks.append((intent_id, status, detail))
