from __future__ import annotations

import sys
import time
from pathlib import Path
from queue import Queue
from threading import Event, Thread, get_ident
from types import SimpleNamespace
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from app.nautilus_actors import (  # noqa: E402
    CommandPollerActor,
    IntentPublisherActor,
)
from execution_domain.control_plane import (  # noqa: E402
    CommandType,
    NodeCommand,
)
from runtime.control_plane_session import NodeControlPlaneSession  # noqa: E402

MAX_CALLBACK_SECONDS = 0.05


def test_blocked_intent_fetch_keeps_actor_timer_callback_bounded() -> None:
    client = _IntentClient()
    fetch_started = Event()
    release_fetch = Event()

    def fetch(capacity: int) -> tuple[Any, ...]:
        del capacity
        fetch_started.set()
        release_fetch.wait(timeout=2.0)
        return ()

    session = NodeControlPlaneSession(
        intent_fetch=fetch,
        intent_deliver=client.deliver,
        intent_fetch_interval_seconds=0.01,
        operation_timeout_seconds=1.0,
    )
    actor = IntentPublisherActor(
        client,
        control_plane_session=session,
        worker_shutdown_wait_seconds=1.0,
    )
    actor.on_start()
    assert fetch_started.wait(timeout=1.0)

    started_at = time.monotonic()
    actor._on_poll_timer()
    elapsed = time.monotonic() - started_at

    release_fetch.set()
    actor.on_stop()
    assert elapsed < MAX_CALLBACK_SECONDS


def test_blocked_plain_intent_poll_keeps_actor_timer_callback_bounded() -> None:
    client = _BlockingIntentClient()
    actor = IntentPublisherActor(client)
    actor_thread_id = get_ident()
    actor.on_start()

    started_at = time.monotonic()
    actor._on_poll_timer()
    first_elapsed = time.monotonic() - started_at
    assert client.poll_started.wait(timeout=1.0)

    started_at = time.monotonic()
    actor._on_poll_timer()
    in_flight_elapsed = time.monotonic() - started_at

    client.release_poll.set()
    assert client.poll_finished.wait(timeout=1.0)
    actor._on_poll_timer()
    actor.on_stop()

    assert first_elapsed < MAX_CALLBACK_SECONDS
    assert in_flight_elapsed < MAX_CALLBACK_SECONDS
    assert client.poll_thread_id is not None
    assert client.poll_thread_id != actor_thread_id


def test_intent_stop_retains_running_poll_cleanup_identity_for_retry() -> None:
    client = _BlockingIntentClient()
    lifecycle = _Lifecycle()
    actor = IntentPublisherActor(
        client,
        lifecycle=lifecycle,
        worker_shutdown_wait_seconds=0.01,
    )
    actor.on_start()
    actor._on_poll_timer()
    assert client.poll_started.wait(timeout=1.0)
    executor = actor._executor
    future = actor._poll_future

    actor.on_stop()
    try:
        assert actor._executor is executor
        assert actor._poll_future is future
        assert _failed_reasons(lifecycle)["intent_stream"] == (
            "approved intent poll worker failed to stop before deadline"
        )
    finally:
        client.release_poll.set()
        assert client.poll_finished.wait(timeout=1.0)
        actor.on_stop()

    assert actor._executor is None
    assert actor._poll_future is None


def test_plain_intent_delivery_publishes_on_actor_timer_thread() -> None:
    client = _OneIntentClient()
    actor = IntentPublisherActor(client)
    actor_thread_id = get_ident()
    published_thread_ids: list[int] = []
    actor.publish = lambda intent: published_thread_ids.append(get_ident())
    actor.on_start()

    assert _pump_actor_until(actor, lambda: bool(published_thread_ids))

    actor.on_stop()
    assert published_thread_ids == [actor_thread_id]
    assert client.poll_thread_id is not None
    assert client.poll_thread_id != actor_thread_id


def test_session_intent_delivery_publishes_on_actor_timer_thread() -> None:
    client = _IntentClient()
    fetched = False

    def fetch(capacity: int) -> tuple[Any, ...]:
        nonlocal fetched
        del capacity
        if fetched:
            return ()
        fetched = True
        return (SimpleNamespace(account_id="account-a"),)

    session = NodeControlPlaneSession(
        intent_fetch=fetch,
        intent_deliver=client.deliver,
        intent_fetch_interval_seconds=0.01,
    )
    actor = IntentPublisherActor(
        client,
        control_plane_session=session,
    )
    actor_thread_id = get_ident()
    published_thread_ids: list[int] = []
    actor.publish = lambda intent: published_thread_ids.append(get_ident())
    actor.on_start()

    assert _pump_actor_until(actor, lambda: bool(published_thread_ids))

    actor.on_stop()
    assert published_thread_ids == [actor_thread_id]
    assert client.delivery_thread_id is not None
    assert client.delivery_thread_id != actor_thread_id


def test_intent_mailbox_full_and_stop_release_waiters() -> None:
    client = _IntentClient()
    lifecycle = _Lifecycle()
    actor = IntentPublisherActor(
        client,
        lifecycle=lifecycle,
        control_plane_session=_LocalSession(),
        pending_intent_limit=1,
        publication_enqueue_timeout_seconds=0.01,
        publication_completion_timeout_seconds=1.0,
    )
    first_errors: list[BaseException] = []

    def deliver_first() -> None:
        try:
            client.deliver(SimpleNamespace(account_id="account-a"))
        except BaseException as exc:
            first_errors.append(exc)

    worker = Thread(target=deliver_first)
    worker.start()
    assert _wait_until(lambda: actor.pending_intent_count == 1)

    with pytest.raises(RuntimeError, match="backlog full"):
        client.deliver(SimpleNamespace(account_id="account-a"))

    actor.on_stop()
    worker.join(timeout=1.0)

    assert worker.is_alive() is False
    assert len(first_errors) == 1
    assert "stopped" in str(first_errors[0])
    assert "backlog full" in actor.failure_reason


def test_intent_stop_rejects_delivery_admitted_during_stop_race() -> None:
    client = _IntentClient()
    actor = IntentPublisherActor(
        client,
        control_plane_session=_LocalSession(),
        publication_completion_timeout_seconds=1.0,
    )
    pending: _AdmissionBarrierQueue = _AdmissionBarrierQueue()
    actor._pending_intents = pending
    actor._queued_publisher._pending = pending
    errors: list[BaseException] = []

    def deliver() -> None:
        try:
            client.deliver(SimpleNamespace(account_id="account-a"))
        except BaseException as exc:
            errors.append(exc)

    worker = Thread(target=deliver)
    worker.start()
    assert pending.put_started.wait(timeout=1.0)

    stopper = Thread(target=actor.on_stop)
    stopper.start()
    pending.release_put.set()
    worker.join(timeout=1.0)
    stopper.join(timeout=1.0)

    assert pending.admitted.is_set()
    assert worker.is_alive() is False
    assert stopper.is_alive() is False
    assert pending.empty()
    assert len(errors) == 1
    assert "stopped" in str(errors[0])


def test_intent_delivery_timeout_cancels_late_publication() -> None:
    client = _IntentClient()
    actor = IntentPublisherActor(
        client,
        control_plane_session=_LocalSession(),
        publication_completion_timeout_seconds=0.02,
    )
    published: list[Any] = []
    actor.publish = published.append

    with pytest.raises(RuntimeError, match="timed out"):
        client.deliver(SimpleNamespace(account_id="account-a"))

    actor._on_poll_timer()
    actor.on_stop()

    assert published == []
    assert "timed out" in actor.failure_reason


def test_intent_session_stop_deadline_failure_fails_stream() -> None:
    lifecycle = _Lifecycle()
    actor = IntentPublisherActor(
        _IntentClient(),
        lifecycle=lifecycle,
        control_plane_session=_LocalSession(stop_result=False),
    )

    actor.on_stop()

    assert _failed_reasons(lifecycle) == {
        "intent_stream": (
            "control-plane session failed to stop before deadline"
        ),
    }


def test_blocked_command_poll_keeps_actor_timer_callback_bounded() -> None:
    control_plane = _BlockingCommandControlPlane()
    lifecycle = _Lifecycle()
    actor_holder: dict[str, CommandPollerActor] = {}
    session = NodeControlPlaneSession(
        command_poll=lambda capacity: actor_holder[
            "actor"
        ].session_poll_commands(capacity),
        command_apply=lambda command: actor_holder[
            "actor"
        ].session_apply_command(command),
        command_ack=lambda acknowledgement: actor_holder[
            "actor"
        ].session_ack_command(acknowledgement),
        command_poll_interval_seconds=0.01,
        operation_timeout_seconds=1.0,
    )
    actor = CommandPollerActor(
        control_plane=control_plane,
        lifecycle=lifecycle,
        node_id="node-a",
        account_id="account-a",
        control_plane_session=session,
        worker_shutdown_wait_seconds=1.0,
    )
    actor_holder["actor"] = actor
    actor.on_start()
    assert control_plane.poll_started.wait(timeout=1.0)

    started_at = time.monotonic()
    actor._on_poll_timer()
    elapsed = time.monotonic() - started_at

    control_plane.release_poll.set()
    actor.on_stop()
    assert elapsed < MAX_CALLBACK_SECONDS


def test_blocked_plain_command_io_keeps_actor_timer_callback_bounded() -> None:
    control_plane = _BlockingCommandControlPlane()
    lifecycle = _Lifecycle()
    actor = CommandPollerActor(
        control_plane=control_plane,
        lifecycle=lifecycle,
        node_id="node-a",
        account_id="account-a",
    )
    actor_thread_id = get_ident()
    actor.on_start()

    started_at = time.monotonic()
    actor._on_poll_timer()
    first_elapsed = time.monotonic() - started_at
    assert control_plane.poll_started.wait(timeout=1.0)
    assert _wait_until(lambda: bool(lifecycle.heartbeat_thread_ids))

    started_at = time.monotonic()
    actor._on_poll_timer()
    in_flight_elapsed = time.monotonic() - started_at

    control_plane.release_poll.set()
    assert control_plane.poll_finished.wait(timeout=1.0)
    actor._on_poll_timer()
    actor.on_stop()

    assert first_elapsed < MAX_CALLBACK_SECONDS
    assert in_flight_elapsed < MAX_CALLBACK_SECONDS
    assert control_plane.poll_thread_id is not None
    assert control_plane.poll_thread_id != actor_thread_id
    assert all(
        thread_id != actor_thread_id
        for thread_id in lifecycle.heartbeat_thread_ids
    )


def test_command_stop_retains_running_poll_cleanup_identity_for_retry() -> None:
    control_plane = _BlockingCommandControlPlane()
    lifecycle = _Lifecycle()
    actor = CommandPollerActor(
        control_plane=control_plane,
        lifecycle=lifecycle,
        node_id="node-a",
        account_id="account-a",
        worker_shutdown_wait_seconds=0.01,
    )
    actor.on_start()
    actor._on_poll_timer()
    assert control_plane.poll_started.wait(timeout=1.0)
    executor = actor._command_executor
    future = actor._command_future

    actor.on_stop()
    try:
        assert actor._command_executor is executor
        assert actor._command_future is future
        assert _failed_reasons(lifecycle)["command_stream"] == (
            "operator command poll worker failed to stop before deadline"
        )
    finally:
        control_plane.release_poll.set()
        assert control_plane.poll_finished.wait(timeout=1.0)
        actor.on_stop()

    assert actor._command_executor is None
    assert actor._command_future is None


def test_plain_command_apply_runs_on_actor_thread_and_ack_runs_on_worker() -> None:
    command = NodeCommand(command_id="command-1", type=CommandType.HALT)
    control_plane = _OneCommandControlPlane(command)
    lifecycle = _Lifecycle()
    actor = CommandPollerActor(
        control_plane=control_plane,
        lifecycle=lifecycle,
        node_id="node-a",
        account_id="account-a",
    )
    actor_thread_id = get_ident()
    actor.on_start()

    assert _pump_actor_until(actor, control_plane.acked.is_set)

    actor.on_stop()
    assert lifecycle.apply_thread_ids == [actor_thread_id]
    assert control_plane.poll_thread_id is not None
    assert control_plane.poll_thread_id != actor_thread_id
    assert control_plane.ack_thread_id is not None
    assert control_plane.ack_thread_id != actor_thread_id


def test_session_command_apply_runs_on_actor_thread_and_ack_runs_on_worker() -> None:
    command = NodeCommand(command_id="command-1", type=CommandType.HALT)
    control_plane = _OneCommandControlPlane(command)
    lifecycle = _Lifecycle()
    actor_holder: dict[str, CommandPollerActor] = {}
    session = NodeControlPlaneSession(
        command_poll=lambda capacity: actor_holder[
            "actor"
        ].session_poll_commands(capacity),
        command_apply=lambda item: actor_holder[
            "actor"
        ].session_apply_command(item),
        command_ack=lambda acknowledgement: actor_holder[
            "actor"
        ].session_ack_command(acknowledgement),
        command_poll_interval_seconds=0.01,
    )
    actor = CommandPollerActor(
        control_plane=control_plane,
        lifecycle=lifecycle,
        node_id="node-a",
        account_id="account-a",
        control_plane_session=session,
    )
    actor_holder["actor"] = actor
    actor_thread_id = get_ident()
    actor.on_start()

    assert _pump_actor_until(actor, control_plane.acked.is_set)

    actor.on_stop()
    assert lifecycle.apply_thread_ids == [actor_thread_id]
    assert control_plane.poll_thread_id is not None
    assert control_plane.poll_thread_id != actor_thread_id
    assert control_plane.ack_thread_id is not None
    assert control_plane.ack_thread_id != actor_thread_id


def test_session_command_repoll_does_not_reapply_while_ack_is_blocked() -> None:
    command = NodeCommand(command_id="command-1", type=CommandType.HALT)
    control_plane = _RepeatingCommandControlPlane(command)
    lifecycle = _Lifecycle()
    actor_holder: dict[str, CommandPollerActor] = {}
    session = NodeControlPlaneSession(
        command_poll=lambda capacity: actor_holder[
            "actor"
        ].session_poll_commands(capacity),
        command_apply=lambda item: actor_holder[
            "actor"
        ].session_apply_command(item),
        command_ack=lambda acknowledgement: actor_holder[
            "actor"
        ].session_ack_command(acknowledgement),
        command_poll_interval_seconds=0.005,
        command_delivery_capacity=16,
        command_ack_capacity=16,
        operation_timeout_seconds=1.0,
    )
    actor = CommandPollerActor(
        control_plane=control_plane,
        lifecycle=lifecycle,
        node_id="node-a",
        account_id="account-a",
        control_plane_session=session,
        worker_shutdown_wait_seconds=1.0,
    )
    actor_holder["actor"] = actor
    actor.on_start()

    assert _pump_actor_until(actor, control_plane.ack_started.is_set)
    assert _pump_actor_until(actor, lambda: control_plane.poll_count >= 3)
    try:
        assert len(lifecycle.apply_thread_ids) == 1
    finally:
        control_plane.release_ack.set()
        assert control_plane.acked.wait(timeout=1.0)
        actor.on_stop()


def test_session_command_ack_failure_keeps_process_local_apply_result() -> None:
    command = NodeCommand(command_id="command-1", type=CommandType.HALT)
    control_plane = _FailingAckControlPlane()
    lifecycle = _Lifecycle()
    actor = CommandPollerActor(
        control_plane=control_plane,
        lifecycle=lifecycle,
        node_id="node-a",
        account_id="account-a",
        control_plane_session=_LocalSession(),
    )

    first_ack = _apply_session_command(actor, command)
    with pytest.raises(RuntimeError, match="ACK unavailable"):
        actor.session_ack_command(first_ack)
    second_ack = _apply_session_command(actor, command)
    actor.on_stop()

    assert second_ack is first_ack
    assert len(lifecycle.apply_thread_ids) == 1


def test_command_mailbox_full_fails_stream_and_stop_releases_waiter() -> None:
    lifecycle = _Lifecycle()
    actor = CommandPollerActor(
        control_plane=object(),
        lifecycle=lifecycle,
        node_id="node-a",
        account_id="account-a",
        control_plane_session=_LocalSession(),
        max_pending_commands=1,
        session_enqueue_timeout_seconds=0.01,
        session_completion_timeout_seconds=1.0,
    )
    command = NodeCommand(command_id="command-1", type=CommandType.HALT)
    first_errors: list[BaseException] = []

    def apply_first() -> None:
        try:
            actor.session_apply_command(command)
        except BaseException as exc:
            first_errors.append(exc)

    worker = Thread(target=apply_first)
    worker.start()
    assert _wait_until(lambda: actor.pending_command_count == 1)

    with pytest.raises(RuntimeError, match="mailbox capacity exceeded"):
        actor.session_apply_command(
            NodeCommand(command_id="command-2", type=CommandType.HALT)
        )

    actor.on_stop()
    worker.join(timeout=1.0)

    assert worker.is_alive() is False
    assert len(first_errors) == 1
    assert "stopped" in str(first_errors[0])
    assert _failed_reasons(lifecycle)["command_stream"] == (
        "operator command actor mailbox capacity exceeded"
    )


def test_command_stop_rejects_delivery_admitted_during_stop_race() -> None:
    lifecycle = _Lifecycle()
    actor = CommandPollerActor(
        control_plane=object(),
        lifecycle=lifecycle,
        node_id="node-a",
        account_id="account-a",
        control_plane_session=_LocalSession(),
        session_completion_timeout_seconds=1.0,
    )
    pending: _AdmissionBarrierQueue = _AdmissionBarrierQueue()
    actor._session_commands = pending
    command = NodeCommand(command_id="command-1", type=CommandType.HALT)
    errors: list[BaseException] = []

    def apply_command() -> None:
        try:
            actor.session_apply_command(command)
        except BaseException as exc:
            errors.append(exc)

    worker = Thread(target=apply_command)
    worker.start()
    assert pending.put_started.wait(timeout=1.0)

    stopper = Thread(target=actor.on_stop)
    stopper.start()
    pending.release_put.set()
    worker.join(timeout=1.0)
    stopper.join(timeout=1.0)

    assert pending.admitted.is_set()
    assert worker.is_alive() is False
    assert stopper.is_alive() is False
    assert pending.empty()
    assert len(errors) == 1
    assert "stopped" in str(errors[0])
    assert lifecycle.apply_thread_ids == []


def test_command_apply_timeout_cancels_late_actor_apply() -> None:
    lifecycle = _Lifecycle()
    actor = CommandPollerActor(
        control_plane=object(),
        lifecycle=lifecycle,
        node_id="node-a",
        account_id="account-a",
        control_plane_session=_LocalSession(),
        session_completion_timeout_seconds=0.02,
    )
    command = NodeCommand(command_id="command-1", type=CommandType.HALT)

    with pytest.raises(RuntimeError, match="timed out"):
        actor.session_apply_command(command)

    actor._on_poll_timer()
    actor.on_stop()

    assert lifecycle.apply_thread_ids == []
    assert _failed_reasons(lifecycle)["command_stream"] == (
        "operator command apply timed out waiting for actor thread"
    )


def test_stopped_command_actor_rejects_session_delivery() -> None:
    actor = CommandPollerActor(
        control_plane=object(),
        lifecycle=_Lifecycle(),
        node_id="node-a",
        account_id="account-a",
        control_plane_session=_LocalSession(),
    )
    actor.on_stop()

    with pytest.raises(RuntimeError, match="stopped"):
        actor.session_apply_command(
            NodeCommand(command_id="command-1", type=CommandType.HALT)
        )


def test_command_session_stop_deadline_failure_fails_dependencies() -> None:
    lifecycle = _Lifecycle()
    actor = CommandPollerActor(
        control_plane=object(),
        lifecycle=lifecycle,
        node_id="node-a",
        account_id="account-a",
        control_plane_session=_LocalSession(stop_result=False),
    )

    actor.on_stop()

    reason = "control-plane session failed to stop before deadline"
    assert _failed_reasons(lifecycle) == {
        "control_plane": reason,
        "command_stream": reason,
    }


def test_stale_intent_session_fails_stream_without_polling_client() -> None:
    client = _IntentClient()
    lifecycle = _Lifecycle()
    actor = IntentPublisherActor(
        client,
        lifecycle=lifecycle,
        control_plane_session=_LocalSession(),
        stale_after_seconds=0,
    )

    actor._on_poll_timer()
    actor.on_stop()

    assert client.poll_calls == []
    assert _failed_reasons(lifecycle) == {
        "intent_stream": "approved intent poll stale",
    }


def test_stale_session_lanes_fail_closed_without_control_plane_calls() -> None:
    lifecycle = _Lifecycle()
    control_plane = _ForbiddenControlPlane()
    session = _LocalSession()
    actor = CommandPollerActor(
        control_plane=control_plane,
        lifecycle=lifecycle,
        node_id="node-a",
        account_id="account-a",
        control_plane_session=session,
        stale_after_seconds=0,
    )

    actor._on_poll_timer()
    actor.on_stop()

    assert control_plane.calls == []
    assert _failed_reasons(lifecycle) == {
        "control_plane": "control-plane heartbeat stale",
        "command_stream": "operator command poll stale",
    }


def test_recoverable_intent_session_failure_is_degraded_until_recovery() -> None:
    lifecycle = _Lifecycle()
    session = _LocalSession()
    fetch_lane = session._snapshot.lanes["intent_fetch"]
    fetch_lane.failure = "intent fetch HTTP 503"
    fetch_lane.circuit_state = "open"
    actor = IntentPublisherActor(
        _IntentClient(),
        lifecycle=lifecycle,
        control_plane_session=session,
        stale_after_seconds=10.0,
    )

    actor._on_poll_timer()

    assert _failed_reasons(lifecycle) == {}
    assert _degraded_reasons(lifecycle) == {
        "intent_stream": "intent fetch HTTP 503",
    }
    assert actor.degraded_reason == "intent fetch HTTP 503"

    fetch_lane.failure = False
    fetch_lane.circuit_state = "closed"
    fetch_lane.last_success_at = time.monotonic()
    actor._on_poll_timer()
    actor.on_stop()

    assert _degraded_reasons(lifecycle) == {}
    assert actor.degraded_reason == ""


def test_recoverable_command_session_failure_is_degraded_until_recovery() -> None:
    lifecycle = _Lifecycle()
    session = _LocalSession()
    heartbeat_lane = session._snapshot.lanes["heartbeat"]
    heartbeat_lane.last_success_at = time.monotonic()
    command_lane = session._snapshot.lanes["command_poll"]
    command_lane.last_success_at = time.monotonic()
    ack_lane = session._snapshot.lanes["command_ack"]
    ack_lane.failure = "command ACK HTTP 503"
    ack_lane.circuit_state = "open"
    actor = CommandPollerActor(
        control_plane=object(),
        lifecycle=lifecycle,
        node_id="node-a",
        account_id="account-a",
        control_plane_session=session,
        stale_after_seconds=10.0,
    )

    actor._on_poll_timer()

    assert _failed_reasons(lifecycle) == {}
    assert _degraded_reasons(lifecycle) == {
        "command_stream": "command ACK HTTP 503",
    }
    assert actor.degraded_reason == "command ACK HTTP 503"

    ack_lane.failure = False
    ack_lane.circuit_state = "closed"
    ack_lane.last_success_at = time.monotonic()
    actor._on_poll_timer()
    actor.on_stop()

    assert _degraded_reasons(lifecycle) == {}
    assert actor.degraded_reason == ""


def test_fatal_session_queue_state_remains_hard_failure() -> None:
    lifecycle = _Lifecycle()
    session = _LocalSession()
    heartbeat_lane = session._snapshot.lanes["heartbeat"]
    heartbeat_lane.last_success_at = time.monotonic()
    command_lane = session._snapshot.lanes["command_poll"]
    command_lane.last_success_at = time.monotonic()
    ack_lane = session._snapshot.lanes["command_ack"]
    ack_lane.failure = "command ACK queue capacity exceeded"
    ack_lane.fatal_failure = "command ACK queue capacity exceeded"
    ack_lane.queue_pressure = "full"
    actor = CommandPollerActor(
        control_plane=object(),
        lifecycle=lifecycle,
        node_id="node-a",
        account_id="account-a",
        control_plane_session=session,
        stale_after_seconds=10.0,
    )

    actor._on_poll_timer()
    actor.on_stop()

    assert _failed_reasons(lifecycle) == {
        "command_stream": "command ACK queue capacity exceeded",
    }
    assert _degraded_reasons(lifecycle) == {}


def _pump_actor_until(
    actor: Any,
    predicate: Any,
    timeout: float = 1.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        actor._on_poll_timer()
        if predicate():
            return True
        time.sleep(0.001)
    return bool(predicate())


def _wait_until(predicate: Any, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return bool(predicate())


def _apply_session_command(
    actor: CommandPollerActor,
    command: NodeCommand,
) -> Any:
    acknowledgements: list[Any] = []
    errors: list[BaseException] = []

    def apply_command() -> None:
        try:
            acknowledgements.append(actor.session_apply_command(command))
        except BaseException as exc:
            errors.append(exc)

    worker = Thread(target=apply_command)
    worker.start()
    assert _pump_actor_until(actor, lambda: not worker.is_alive())
    worker.join(timeout=1.0)
    assert errors == []
    assert len(acknowledgements) == 1
    return acknowledgements[0]


def _failed_reasons(lifecycle: _Lifecycle) -> dict[str, str]:
    return {
        dependency.value: reason
        for dependency, reason in lifecycle.failed_dependencies
    }


def _degraded_reasons(lifecycle: _Lifecycle) -> dict[str, str]:
    return {
        dependency.value: reason
        for dependency, reason in lifecycle.degraded_dependencies.items()
    }


class _AttachablePublisher:
    def __init__(self) -> None:
        self._publishers: list[Any] = []

    def attach(self, publisher: Any) -> None:
        self._publishers.append(publisher)

    def publish(self, item: Any) -> None:
        for publisher in self._publishers:
            publisher.publish(item)


class _AdmissionBarrierQueue(Queue[Any]):
    def __init__(self) -> None:
        super().__init__()
        self.put_started = Event()
        self.release_put = Event()
        self.admitted = Event()

    def put(
        self,
        item: Any,
        block: bool = True,
        timeout: float | None = None,
    ) -> None:
        self.put_started.set()
        self.release_put.wait(timeout=1.0)
        super().put(item, block=block, timeout=timeout)
        self.admitted.set()


class _IntentClient:
    def __init__(self) -> None:
        self._publisher = _AttachablePublisher()
        self.delivery_thread_id: int | None = None
        self.poll_calls: list[tuple[int, int]] = []

    def deliver(self, item: Any) -> None:
        self.delivery_thread_id = get_ident()
        self._publisher.publish(item)

    def poll_once(self, limit: int = 100, wait_ms: int = 0) -> int:
        self.poll_calls.append((limit, wait_ms))
        return 0


class _BlockingIntentClient(_IntentClient):
    def __init__(self) -> None:
        super().__init__()
        self.poll_started = Event()
        self.poll_finished = Event()
        self.release_poll = Event()
        self.poll_thread_id: int | None = None

    def poll_once(self, limit: int = 100, wait_ms: int = 0) -> int:
        del limit, wait_ms
        self.poll_thread_id = get_ident()
        self.poll_started.set()
        self.release_poll.wait(timeout=2.0)
        self.poll_finished.set()
        return 0


class _OneIntentClient(_IntentClient):
    def __init__(self) -> None:
        super().__init__()
        self._polled = False
        self.poll_thread_id: int | None = None

    def poll_once(self, limit: int = 100, wait_ms: int = 0) -> int:
        self.poll_calls.append((limit, wait_ms))
        self.poll_thread_id = get_ident()
        if self._polled:
            return 0
        self._polled = True
        self.deliver(SimpleNamespace(account_id="account-a"))
        return 1


class _Lifecycle:
    def __init__(self) -> None:
        self.apply_thread_ids: list[int] = []
        self.heartbeat_thread_ids: list[int] = []
        self.ready_dependencies: list[Any] = []
        self.failed_dependencies: list[tuple[Any, str]] = []
        self.degraded_dependencies: dict[Any, str] = {}

    def send_heartbeat(self) -> None:
        self.heartbeat_thread_ids.append(get_ident())

    def apply_operator_state(self, state: Any, reason: str) -> None:
        del state, reason
        self.apply_thread_ids.append(get_ident())

    def mark_dependency_ready(self, dependency: Any) -> None:
        self.ready_dependencies.append(dependency)
        self.degraded_dependencies.pop(dependency, None)

    def mark_dependency_failed(self, dependency: Any, reason: str) -> None:
        self.failed_dependencies.append((dependency, reason))
        self.degraded_dependencies.pop(dependency, None)

    def mark_dependency_degraded(
        self,
        dependency: Any,
        reason: str,
    ) -> None:
        self.degraded_dependencies[dependency] = reason


class _LocalSession:
    def __init__(self, *, stop_result: bool = True) -> None:
        self.started = False
        self.stopped = False
        self._stop_result = stop_result
        empty_lane = SimpleNamespace(
            failure=False,
            fatal_failure=False,
            last_success_at=False,
            circuit_state="closed",
            queue_pressure="normal",
        )
        self._snapshot = SimpleNamespace(
            degraded=False,
            stopped=False,
            lanes={
                name: SimpleNamespace(**vars(empty_lane))
                for name in (
                    "heartbeat",
                    "command_poll",
                    "command_delivery",
                    "command_ack",
                    "intent_fetch",
                    "intent_delivery",
                    "execution_event",
                )
            }
        )

    def start(self) -> None:
        self.started = True

    def stop(self, deadline: float) -> bool:
        del deadline
        self.stopped = True
        return self._stop_result

    def snapshot(self) -> Any:
        return self._snapshot


class _BlockingCommandControlPlane:
    def __init__(self) -> None:
        self.poll_started = Event()
        self.poll_finished = Event()
        self.release_poll = Event()
        self.poll_thread_id: int | None = None

    def poll_commands(self, node_id: str, after: Any) -> tuple[Any, ...]:
        del node_id, after
        self.poll_thread_id = get_ident()
        self.poll_started.set()
        self.release_poll.wait(timeout=2.0)
        self.poll_finished.set()
        return ()

    def ack_command(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs


class _OneCommandControlPlane:
    def __init__(self, command: Any) -> None:
        self._command = command
        self._polled = False
        self.acked = Event()
        self.poll_thread_id: int | None = None
        self.ack_thread_id: int | None = None

    def poll_commands(self, node_id: str, after: Any) -> tuple[Any, ...]:
        del node_id, after
        self.poll_thread_id = get_ident()
        if self._polled:
            return ()
        self._polled = True
        return (self._command,)

    def ack_command(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self.ack_thread_id = get_ident()
        self.acked.set()


class _RepeatingCommandControlPlane:
    def __init__(self, command: Any) -> None:
        self._command = command
        self.poll_count = 0
        self.ack_started = Event()
        self.release_ack = Event()
        self.acked = Event()

    def poll_commands(self, node_id: str, after: Any) -> tuple[Any, ...]:
        del node_id, after
        self.poll_count += 1
        if self.acked.is_set():
            return ()
        return (self._command,)

    def ack_command(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self.ack_started.set()
        self.release_ack.wait(timeout=2.0)
        self.acked.set()


class _FailingAckControlPlane:
    def ack_command(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError("ACK unavailable")


class _ForbiddenControlPlane:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def heartbeat(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self.calls.append("heartbeat")
        raise AssertionError("actor callback performed heartbeat I/O")

    def poll_commands(self, *args: Any, **kwargs: Any) -> tuple[Any, ...]:
        del args, kwargs
        self.calls.append("poll_commands")
        raise AssertionError("actor callback performed command I/O")

    def ack_command(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self.calls.append("ack_command")
        raise AssertionError("actor callback performed ACK I/O")
