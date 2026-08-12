from __future__ import annotations

import sys
import time
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, current_thread
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from app.nautilus_actors import (  # noqa: E402
    CommandPollerActor,
    _CommandPersistenceTask,
)
from execution_domain.control_plane import (  # noqa: E402
    CommandAckStatus,
    CommandType,
    NodeCommand,
    TradingState,
)


NOW = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)


class _Lifecycle:
    def __init__(self, runtime_generation: str) -> None:
        self.runtime_generation = runtime_generation
        self.reconciliation_generation = 7
        self.lease_generation = 11
        self.applied_states: list[TradingState] = []

    def apply_operator_state(self, state: TradingState, reason: str) -> None:
        del reason
        self.applied_states.append(state)

    def validate_risk_generation(
        self,
        *,
        runtime_generation: str,
        reconciliation_generation: int,
        lease_generation: int,
    ) -> None:
        assert runtime_generation == self.runtime_generation
        assert reconciliation_generation == self.reconciliation_generation
        assert lease_generation == self.lease_generation


class _LiveLifecycle(_Lifecycle):
    def __init__(
        self,
        runtime_generation: str,
        *,
        trusted_gate: dict[str, Any],
    ) -> None:
        super().__init__(runtime_generation)
        self.config = SimpleNamespace(
            binance=SimpleNamespace(environment="live")
        )
        self._trusted_gate = trusted_gate
        self.validated_gates: list[
            tuple[dict[str, Any] | bool, bool]
        ] = []

    def validate_live_open_gate(
        self,
        raw_gate: dict[str, Any] | bool,
        *,
        require_normal: bool,
    ) -> None:
        self.validated_gates.append((raw_gate, require_normal))
        if raw_gate != self._trusted_gate:
            raise RuntimeError("live_open_gate_mismatch")
        if require_normal and raw_gate.get("mode") != "normal":
            raise RuntimeError("live_open_gate_not_normal")


class _ControlPlane:
    def __init__(self) -> None:
        self.acks: list[tuple[str, CommandAckStatus, str | None]] = []
        self.ack_threads: list[str] = []

    def ack_command(
        self,
        node_id: str,
        command_id: str,
        status: CommandAckStatus,
        *,
        error: str | None = None,
    ) -> None:
        del node_id
        self.acks.append((command_id, status, error))
        self.ack_threads.append(current_thread().name)


class _FailOnceControlPlane(_ControlPlane):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def ack_command(
        self,
        node_id: str,
        command_id: str,
        status: CommandAckStatus,
        *,
        error: str | None = None,
    ) -> None:
        self.attempts += 1
        if self.attempts == 1:
            raise OSError("temporary ACK failure")
        super().ack_command(
            node_id,
            command_id,
            status,
            error=error,
        )


class _Watchdog:
    def start(self) -> None:
        return

    def record_tick(self) -> None:
        return

    def stop(self) -> None:
        return


class _Session:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self, deadline: float) -> bool:
        del deadline
        self.stopped += 1
        return True


def test_applied_resume_restarts_as_ack_only_without_reapplying(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "commands.json"
    command = NodeCommand(
        command_id="resume-1",
        type=CommandType.RESUME,
        issued_at=NOW,
    )
    first_lifecycle = _Lifecycle("runtime-a")
    first = _actor(
        journal_path,
        first_lifecycle,
        _ControlPlane(),
        command_now=lambda: NOW,
    )
    first._pending_commands = (command,)

    assert first._apply_pending_commands() == 1
    assert first_lifecycle.applied_states == [TradingState.ACTIVE]
    first.on_stop()

    second_control_plane = _ControlPlane()
    second_lifecycle = _Lifecycle("runtime-b")
    second = _actor(
        journal_path,
        second_lifecycle,
        second_control_plane,
        command_now=lambda: NOW,
    )
    second._pending_commands = (command,)

    assert second._apply_pending_commands() == 0
    assert second_lifecycle.applied_states == []
    second._submit_acks()
    assert _wait_until(lambda: second._ack_future is not None)
    assert _wait_until(lambda: bool(second._ack_future.done()))
    second._harvest_acks()

    assert second_control_plane.acks == [
        ("resume-1", CommandAckStatus.COMPLETED, None)
    ]
    second.on_stop()


def test_startup_actively_sends_restored_ack_without_command_redelivery(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "commands.json"
    command = NodeCommand(command_id="startup-ack", type=CommandType.HALT)
    first = _actor(journal_path, _Lifecycle("runtime-a"), _ControlPlane())
    first._pending_commands = (command,)

    assert first._apply_pending_commands() == 1
    first.on_stop()

    control_plane = _ControlPlane()
    session = _Session()
    restarted = CommandPollerActor(
        control_plane=control_plane,
        lifecycle=_Lifecycle("runtime-b"),
        node_id="node-a",
        account_id="account-a",
        command_journal_path=journal_path,
        control_plane_session=session,
    )
    restarted._tick_watchdog = _Watchdog()

    restarted.on_start()

    assert _wait_until(lambda: len(control_plane.acks) == 1)
    assert control_plane.acks == [
        ("startup-ack", CommandAckStatus.COMPLETED, None)
    ]
    assert control_plane.ack_threads[0].startswith(
        "operator-commands.poll.ack"
    )
    assert session.started == 1
    restarted.on_stop()

    final_control_plane = _ControlPlane()
    final = _actor(
        journal_path,
        _Lifecycle("runtime-c"),
        final_control_plane,
    )
    final._tick_watchdog = _Watchdog()
    final.on_start()
    time.sleep(0.02)

    assert final_control_plane.acks == []
    final.on_stop()


def test_duplicate_ack_submission_is_idempotent_by_command_id(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "commands.json"
    command = NodeCommand(command_id="duplicate-ack", type=CommandType.HALT)
    control_plane = _ControlPlane()
    actor = _actor(
        journal_path,
        _Lifecycle("runtime-a"),
        control_plane,
    )
    actor._pending_commands = (command,)

    assert actor._apply_pending_commands() == 1
    acknowledgement = actor._pending_acks[command.command_id]
    actor.session_ack_command(acknowledgement)
    actor.session_ack_command(acknowledgement)

    assert control_plane.acks == [
        ("duplicate-ack", CommandAckStatus.COMPLETED, None)
    ]
    actor.on_stop()


def test_failed_startup_ack_remains_durable_and_retries(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "commands.json"
    command = NodeCommand(command_id="retry-ack", type=CommandType.HALT)
    first = _actor(journal_path, _Lifecycle("runtime-a"), _ControlPlane())
    first._pending_commands = (command,)
    assert first._apply_pending_commands() == 1
    first.on_stop()

    control_plane = _FailOnceControlPlane()
    restarted = _actor(
        journal_path,
        _Lifecycle("runtime-b"),
        control_plane,
    )
    restarted._tick_watchdog = _Watchdog()
    restarted.on_start()
    assert _wait_until(
        lambda: restarted._ack_future is not None
        and restarted._ack_future.done()
    )

    restarted._harvest_acks()
    assert command.command_id in restarted._pending_acks
    restarted._submit_acks()
    assert _wait_until(
        lambda: restarted._ack_future is not None
        and restarted._ack_future.done()
    )
    restarted._harvest_acks()

    assert control_plane.attempts == 2
    assert control_plane.acks == [
        ("retry-ack", CommandAckStatus.COMPLETED, None)
    ]
    assert command.command_id not in restarted._pending_acks
    restarted.on_stop()


def test_restored_ack_backlog_over_capacity_triggers_fatal_fence(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "commands.json"
    first = _actor(journal_path, _Lifecycle("runtime-a"), _ControlPlane())
    commands = (
        NodeCommand(command_id="backlog-1", type=CommandType.HALT),
        NodeCommand(command_id="backlog-2", type=CommandType.HALT),
    )
    first._pending_commands = commands
    assert first._apply_pending_commands() == 2
    first.on_stop()

    fatal_reasons: list[str] = []
    restarted = CommandPollerActor(
        control_plane=_ControlPlane(),
        lifecycle=_Lifecycle("runtime-b"),
        node_id="node-a",
        account_id="account-a",
        command_journal_path=journal_path,
        max_pending_acks=1,
        fatal_callback=fatal_reasons.append,
    )

    assert restarted._stopped.is_set()
    assert fatal_reasons == [
        "command ACK durable outbox capacity exceeded"
    ]
    restarted.on_stop()


def test_oversized_command_payload_triggers_journal_byte_fence(
    tmp_path: Path,
) -> None:
    fatal_reasons: list[str] = []
    actor = CommandPollerActor(
        control_plane=_ControlPlane(),
        lifecycle=_Lifecycle("runtime-a"),
        node_id="node-a",
        account_id="account-a",
        command_journal_path=tmp_path / "commands.json",
        command_journal_max_bytes=512,
        fatal_callback=fatal_reasons.append,
    )
    actor._pending_commands = (
        NodeCommand(
            command_id="oversized",
            type=CommandType.HALT,
            args={"payload": "x" * 2048},
        ),
    )

    assert actor._apply_pending_commands() == 0
    assert actor._stopped.is_set()
    assert len(fatal_reasons) == 1
    assert "command journal byte capacity exceeded" in fatal_reasons[0]
    actor.on_stop()


def test_fresh_resume_applies_within_configured_max_age(
    tmp_path: Path,
) -> None:
    lifecycle = _Lifecycle("runtime-a")
    actor = _actor(
        tmp_path / "commands.json",
        lifecycle,
        _ControlPlane(),
        command_now=lambda: NOW,
        resume_command_max_age_seconds=30.0,
    )
    actor._pending_commands = (
        NodeCommand(
            command_id="fresh-resume",
            type=CommandType.RESUME,
            issued_at=NOW - timedelta(seconds=29),
        ),
    )

    assert actor._apply_pending_commands() == 1
    assert lifecycle.applied_states == [TradingState.ACTIVE]
    acknowledgement = actor._pending_acks["fresh-resume"]
    assert acknowledgement.status is CommandAckStatus.COMPLETED
    actor.on_stop()


def test_expired_pending_resume_stays_halted_and_returns_failed_ack(
    tmp_path: Path,
) -> None:
    lifecycle = _Lifecycle("runtime-a")
    actor = _actor(
        tmp_path / "commands.json",
        lifecycle,
        _ControlPlane(),
        command_now=lambda: NOW,
        resume_command_max_age_seconds=30.0,
    )
    actor._pending_commands = (
        NodeCommand(
            command_id="expired-resume",
            type=CommandType.RESUME,
            issued_at=NOW - timedelta(seconds=31),
        ),
    )

    assert actor._apply_pending_commands() == 1
    assert lifecycle.applied_states == [TradingState.HALTED]
    acknowledgement = actor._pending_acks["expired-resume"]
    assert acknowledgement.status is CommandAckStatus.FAILED
    assert acknowledgement.error == (
        "resume command expired: age 31.000s exceeds 30.000s"
    )
    actor.on_stop()


def test_live_resume_requires_matching_normal_gate(
    tmp_path: Path,
) -> None:
    gate = _normal_live_open_gate()
    lifecycle = _LiveLifecycle("runtime-a", trusted_gate=gate)
    actor = _actor(
        tmp_path / "commands.json",
        lifecycle,
        _ControlPlane(),
        command_now=lambda: NOW,
    )
    actor._pending_commands = (
        NodeCommand(
            command_id="normal-resume",
            type=CommandType.RESUME,
            issued_at=NOW,
            args={
                "account_id": "account-a",
                "live_open_gate": gate,
            },
        ),
    )

    assert actor._apply_pending_commands() == 1
    assert lifecycle.validated_gates == [(gate, True)]
    assert lifecycle.applied_states == [TradingState.ACTIVE]
    actor.on_stop()


def test_live_resume_rejects_missing_gate_and_stays_halted(
    tmp_path: Path,
) -> None:
    lifecycle = _LiveLifecycle(
        "runtime-a",
        trusted_gate=_normal_live_open_gate(),
    )
    actor = _actor(
        tmp_path / "commands.json",
        lifecycle,
        _ControlPlane(),
        command_now=lambda: NOW,
    )
    actor._pending_commands = (
        NodeCommand(
            command_id="missing-gate-resume",
            type=CommandType.RESUME,
            issued_at=NOW,
            args={"account_id": "account-a"},
        ),
    )

    assert actor._apply_pending_commands() == 1
    assert lifecycle.applied_states == [TradingState.HALTED]
    acknowledgement = actor._pending_acks["missing-gate-resume"]
    assert acknowledgement.status is CommandAckStatus.FAILED
    assert acknowledgement.error == "live_open_gate_mismatch"
    actor.on_stop()


def test_live_canary_resume_accepts_matching_canary_gate(
    tmp_path: Path,
) -> None:
    gate = _canary_live_open_gate()
    lifecycle = _LiveLifecycle("runtime-a", trusted_gate=gate)
    actor = _actor(
        tmp_path / "commands.json",
        lifecycle,
        _ControlPlane(),
        command_now=lambda: NOW,
    )
    actor._pending_commands = (
        NodeCommand(
            command_id="canary-resume",
            type=CommandType.RESUME,
            issued_at=NOW,
            args={
                "account_id": "account-a",
                "live_open_gate": gate,
                "canary_permit": {"permit_id": "permit-a"},
            },
        ),
    )

    assert actor._apply_pending_commands() == 1
    assert lifecycle.validated_gates == [(gate, False)]
    assert lifecycle.applied_states == [TradingState.ACTIVE]
    actor.on_stop()


def test_missing_resume_issued_at_stays_halted_and_returns_failed_ack(
    tmp_path: Path,
) -> None:
    lifecycle = _Lifecycle("runtime-a")
    actor = _actor(
        tmp_path / "commands.json",
        lifecycle,
        _ControlPlane(),
        command_now=lambda: NOW,
    )
    actor._pending_commands = (
        NodeCommand(
            command_id="missing-issued-at",
            type=CommandType.RESUME,
        ),
    )

    assert actor._apply_pending_commands() == 1
    assert lifecycle.applied_states == [TradingState.HALTED]
    acknowledgement = actor._pending_acks["missing-issued-at"]
    assert acknowledgement.status is CommandAckStatus.FAILED
    assert acknowledgement.error == "resume command issued_at is required"
    actor.on_stop()


def test_expired_restored_resume_rewrites_completed_ack_as_failed(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "commands.json"
    command = NodeCommand(
        command_id="expired-restored-resume",
        type=CommandType.RESUME,
        issued_at=NOW,
    )
    first_lifecycle = _Lifecycle("runtime-a")
    first = _actor(
        journal_path,
        first_lifecycle,
        _ControlPlane(),
        command_now=lambda: NOW,
        resume_command_max_age_seconds=30.0,
    )
    first._pending_commands = (command,)
    assert first._apply_pending_commands() == 1
    assert first_lifecycle.applied_states == [TradingState.ACTIVE]
    first.on_stop()

    control_plane = _ControlPlane()
    restarted_lifecycle = _Lifecycle("runtime-b")
    restarted = _actor(
        journal_path,
        restarted_lifecycle,
        control_plane,
        command_now=lambda: NOW + timedelta(seconds=31),
        resume_command_max_age_seconds=30.0,
    )
    restarted._tick_watchdog = _Watchdog()
    restarted.on_start()

    assert _wait_until(lambda: len(control_plane.acks) == 1)
    assert restarted_lifecycle.applied_states == [TradingState.HALTED]
    assert control_plane.acks == [
        (
            "expired-restored-resume",
            CommandAckStatus.FAILED,
            "resume command expired: age 31.000s exceeds 30.000s",
        )
    ]
    restarted.on_stop()


def test_received_resume_with_unknown_outcome_becomes_failed_ack_after_restart(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "commands.json"
    command = NodeCommand(
        command_id="resume-uncertain",
        type=CommandType.RESUME,
        issued_at=NOW,
    )
    first = _actor(
        journal_path,
        _Lifecycle("runtime-a"),
        _ControlPlane(),
        command_now=lambda: NOW,
    )
    first._command_journal.record_received(
        command,
        first._command_generation_context(),
    )

    control_plane = _ControlPlane()
    lifecycle = _Lifecycle("runtime-b")
    restarted = _actor(
        journal_path,
        lifecycle,
        control_plane,
        command_now=lambda: NOW,
    )
    restarted._pending_commands = (command,)

    assert restarted._apply_pending_commands() == 0
    assert lifecycle.applied_states == []
    restarted._submit_acks()
    assert _wait_until(lambda: restarted._ack_future is not None)
    assert _wait_until(lambda: bool(restarted._ack_future.done()))
    restarted._harvest_acks()

    assert control_plane.acks == [
        (
            "resume-uncertain",
            CommandAckStatus.FAILED,
            "command outcome uncertain after runtime restart",
        )
    ]
    restarted.on_stop()


def test_completed_command_replay_sends_ack_without_reapplying(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "commands.json"
    command = NodeCommand(
        command_id="resume-complete",
        type=CommandType.RESUME,
        issued_at=NOW,
    )
    first_control_plane = _ControlPlane()
    first_lifecycle = _Lifecycle("runtime-a")
    first = _actor(
        journal_path,
        first_lifecycle,
        first_control_plane,
        command_now=lambda: NOW,
    )
    first._pending_commands = (command,)
    assert first._apply_pending_commands() == 1
    first._submit_acks()
    assert _wait_until(lambda: first._ack_future is not None)
    assert _wait_until(lambda: bool(first._ack_future.done()))
    first._harvest_acks()
    first.on_stop()

    second_control_plane = _ControlPlane()
    second_lifecycle = _Lifecycle("runtime-b")
    second = _actor(
        journal_path,
        second_lifecycle,
        second_control_plane,
        command_now=lambda: NOW,
    )
    second._pending_commands = (command,)

    assert second._apply_pending_commands() == 0
    assert second_lifecycle.applied_states == []
    second._submit_acks()
    assert _wait_until(lambda: second._ack_future is not None)
    assert _wait_until(lambda: bool(second._ack_future.done()))
    second._harvest_acks()

    assert second_control_plane.acks == [
        ("resume-complete", CommandAckStatus.COMPLETED, None)
    ]
    second.on_stop()


def test_command_persistence_queue_degrades_then_sticky_halts_at_capacity(
    tmp_path: Path,
) -> None:
    fatal_reasons: list[str] = []
    actor = CommandPollerActor(
        control_plane=_ControlPlane(),
        lifecycle=_Lifecycle("runtime-a"),
        node_id="node-a",
        account_id="account-a",
        command_journal_path=tmp_path / "commands.json",
        command_persistence_capacity=5,
        command_persistence_degraded_ratio=0.8,
        fatal_callback=fatal_reasons.append,
    )
    started = Event()
    release = Event()
    original_execute = actor._execute_command_persistence_task

    def blocking_execute(task: _CommandPersistenceTask) -> None:
        started.set()
        release.wait(timeout=1.0)
        original_execute(task)

    actor._execute_command_persistence_task = blocking_execute

    assert actor._submit_command_persistence(
        _CommandPersistenceTask(operation="attempts")
    )
    assert started.wait(timeout=1.0)
    for _index in range(4):
        assert actor._submit_command_persistence(
            _CommandPersistenceTask(operation="attempts")
        )
    assert actor.command_persistence_degraded is True
    assert fatal_reasons == []

    assert actor._submit_command_persistence(
        _CommandPersistenceTask(operation="attempts")
    )
    assert "command persistence queue reached capacity" in fatal_reasons
    assert actor._submit_command_persistence(
        _CommandPersistenceTask(operation="attempts")
    ) is False

    release.set()
    assert _wait_until(
        lambda: actor._command_persistence_queue.unfinished_tasks == 0
    )
    actor.on_stop()


def test_command_persistence_shutdown_obeys_drain_deadline(
    tmp_path: Path,
) -> None:
    fatal_reasons: list[str] = []
    actor = CommandPollerActor(
        control_plane=_ControlPlane(),
        lifecycle=_Lifecycle("runtime-a"),
        node_id="node-a",
        account_id="account-a",
        command_journal_path=tmp_path / "commands.json",
        worker_shutdown_wait_seconds=0.02,
        fatal_callback=fatal_reasons.append,
    )
    started = Event()
    release = Event()
    original_execute = actor._execute_command_persistence_task

    def blocking_execute(task: _CommandPersistenceTask) -> None:
        started.set()
        release.wait(timeout=1.0)
        original_execute(task)

    actor._execute_command_persistence_task = blocking_execute
    assert actor._submit_command_persistence(
        _CommandPersistenceTask(operation="attempts")
    )
    assert started.wait(timeout=1.0)

    started_at = time.monotonic()
    actor.on_stop()
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.08
    assert any(
        "command persistence worker shutdown deadline exceeded" in reason
        for reason in fatal_reasons
    )
    release.set()
    assert _wait_until(
        lambda: not actor._command_persistence_thread.is_alive()
    )


def test_applied_write_failure_restarts_as_unknown_without_reapply(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "commands.json"
    command = NodeCommand(command_id="halt-uncertain", type=CommandType.HALT)
    first_lifecycle = _Lifecycle("runtime-a")
    first = _actor(journal_path, first_lifecycle, _ControlPlane())
    first._command_journal.record_received(
        command,
        first._command_generation_context(),
    )

    def fail_record_applied(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise OSError("applied fsync failed")

    monkeypatch.setattr(
        first._command_journal,
        "record_applied",
        fail_record_applied,
    )
    first._pending_commands = (command,)

    assert first._schedule_pending_commands() == 1
    assert first_lifecycle.applied_states == [TradingState.HALTED]
    durability = first._pending_acks[command.command_id].durability
    assert durability.completed.wait(timeout=1.0)
    first._harvest_command_persistence()
    assert "command journal persistence failed" in first._fatal_reason
    first.on_stop()

    control_plane = _ControlPlane()
    restarted_lifecycle = _Lifecycle("runtime-b")
    restarted = _actor(
        journal_path,
        restarted_lifecycle,
        control_plane,
    )
    restarted._pending_commands = (command,)

    assert restarted._apply_pending_commands() == 0
    assert restarted_lifecycle.applied_states == []
    restarted._submit_acks()
    assert _wait_until(lambda: restarted._ack_future is not None)
    assert _wait_until(lambda: bool(restarted._ack_future.done()))
    restarted._harvest_acks()

    assert control_plane.acks == [
        (
            "halt-uncertain",
            CommandAckStatus.FAILED,
            "command outcome uncertain after runtime restart",
        )
    ]
    restarted.on_stop()


def _actor(
    journal_path: Path,
    lifecycle: _Lifecycle,
    control_plane: Any,
    **kwargs: Any,
) -> CommandPollerActor:
    return CommandPollerActor(
        control_plane=control_plane,
        lifecycle=lifecycle,
        node_id="node-a",
        account_id="account-a",
        command_journal_path=journal_path,
        **kwargs,
    )


def _wait_until(predicate: Any, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return bool(predicate())


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
