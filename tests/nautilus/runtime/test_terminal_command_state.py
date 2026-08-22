from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from app.nautilus_actors import CommandPollerActor  # noqa: E402
from execution_domain.control_plane import (  # noqa: E402
    CommandAckStatus,
    CommandType,
    NodeCommand,
    TradingState,
)


AUTHORIZATION = {
    "authorized_by_type": "user",
    "authorized_by_id": "risk-admin",
    "source_message_id": "terminal-command-test",
}


class _Lifecycle:
    runtime_generation = "runtime-a"
    reconciliation_generation = 3
    lease_generation = 5

    def __init__(self) -> None:
        self.states: list[TradingState] = []

    def apply_operator_state(
        self,
        state: TradingState,
        reason: str,
    ) -> None:
        del reason
        self.states.append(state)

    def validate_risk_generation(self, **kwargs: Any) -> None:
        del kwargs


class _ControlPlane:
    def __init__(self) -> None:
        self.acks: list[
            tuple[str, CommandAckStatus, dict[str, Any] | None, str | None]
        ] = []

    def ack_command(
        self,
        node_id: str,
        command_id: str,
        status: CommandAckStatus,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        del node_id
        self.acks.append((command_id, status, result, error))


class _MessageBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []

    def publish(self, topic: str, msg: Any) -> None:
        self.published.append((topic, msg))


class _EvidenceProvider:
    def __init__(
        self,
        snapshots: list[dict[str, Any]],
    ) -> None:
        self._snapshots = list(snapshots)
        self.force_refreshes: list[bool] = []

    def snapshot(self, *, force_refresh: bool = False) -> dict[str, Any]:
        self.force_refreshes.append(force_refresh)
        if len(self._snapshots) > 1:
            return self._snapshots.pop(0)
        return self._snapshots[0]


class _Actor(CommandPollerActor):
    def __init__(
        self,
        *,
        control_plane: _ControlPlane,
        lifecycle: _Lifecycle,
        message_bus: _MessageBus,
        evidence_provider: _EvidenceProvider,
        journal_path: Path,
    ) -> None:
        self._bound_message_bus = message_bus
        super().__init__(
            control_plane=control_plane,
            lifecycle=lifecycle,
            node_id="node-a",
            account_id="account-a",
            exchange_evidence_provider=evidence_provider,
            command_journal_path=journal_path,
            terminal_verify_attempts=2,
            terminal_verify_delay_seconds=0,
        )

    def _message_bus(self) -> Any:
        return self._bound_message_bus


def test_cancel_all_acks_running_then_completed_after_two_clean_snapshots(
    tmp_path: Path,
) -> None:
    now = datetime.now(timezone.utc)
    control_plane = _ControlPlane()
    message_bus = _MessageBus()
    provider = _EvidenceProvider([_snapshot(now), _snapshot(now)])
    actor = _Actor(
        control_plane=control_plane,
        lifecycle=_Lifecycle(),
        message_bus=message_bus,
        evidence_provider=provider,
        journal_path=tmp_path / "commands.json",
    )
    command = _command("cancel-clean", CommandType.CANCEL_ALL)
    actor._pending_commands = (command,)

    assert actor._apply_pending_commands() == 1
    _flush_ack(actor)
    assert [ack[1] for ack in control_plane.acks] == [
        CommandAckStatus.RUNNING
    ]

    actor._on_terminal_command_result(
        _result_payload(command, dispatched_at=now - timedelta(seconds=1))
    )
    assert actor._drain_terminal_command_results() == 1
    actor._submit_terminal_verification()
    assert _wait_until(lambda: bool(actor._terminal_future.done()))
    actor._harvest_terminal_verification()
    _flush_ack(actor)

    assert [ack[1] for ack in control_plane.acks] == [
        CommandAckStatus.RUNNING,
        CommandAckStatus.COMPLETED,
    ]
    assert provider.force_refreshes == [True, True]
    terminal_result = control_plane.acks[-1][2]
    assert terminal_result
    assert terminal_result["verification"]["clean_streak"] == 2
    actor.on_stop()


def test_close_all_fails_when_target_position_remains(
    tmp_path: Path,
) -> None:
    now = datetime.now(timezone.utc)
    residual = _snapshot(
        now,
        positions=[{"symbol": "SOLUSDT", "quantity": "0.1"}],
    )
    control_plane = _ControlPlane()
    lifecycle = _Lifecycle()
    actor = _Actor(
        control_plane=control_plane,
        lifecycle=lifecycle,
        message_bus=_MessageBus(),
        evidence_provider=_EvidenceProvider([residual, residual]),
        journal_path=tmp_path / "commands.json",
    )
    command = _command("close-residual", CommandType.CLOSE_ALL)
    actor._pending_commands = (command,)

    assert actor._apply_pending_commands() == 1
    assert lifecycle.states == [TradingState.REDUCING]
    _flush_ack(actor)
    actor._on_terminal_command_result(
        _result_payload(command, dispatched_at=now - timedelta(seconds=1))
    )
    actor._drain_terminal_command_results()
    actor._submit_terminal_verification()
    assert _wait_until(lambda: bool(actor._terminal_future.done()))
    actor._harvest_terminal_verification()
    _flush_ack(actor)

    assert [ack[1] for ack in control_plane.acks] == [
        CommandAckStatus.RUNNING,
        CommandAckStatus.FAILED,
    ]
    assert control_plane.acks[-1][3] == (
        "terminal exchange state contains residual risk"
    )
    actor.on_stop()


def test_close_all_rejects_channel_authorization_before_reducing(
    tmp_path: Path,
) -> None:
    now = datetime.now(timezone.utc)
    control_plane = _ControlPlane()
    lifecycle = _Lifecycle()
    message_bus = _MessageBus()
    actor = _Actor(
        control_plane=control_plane,
        lifecycle=lifecycle,
        message_bus=message_bus,
        evidence_provider=_EvidenceProvider([_snapshot(now)]),
        journal_path=tmp_path / "commands.json",
    )
    command = _command(
        "channel-close",
        CommandType.CLOSE_ALL,
        authorized_by_type="channel",
    )
    actor._pending_commands = (command,)

    assert actor._apply_pending_commands() == 1
    _flush_ack(actor)

    assert lifecycle.states == []
    assert message_bus.published == []
    assert control_plane.acks[-1][1] is CommandAckStatus.FAILED
    assert control_plane.acks[-1][3] == "user_authorization_required"
    actor.on_stop()


def test_running_terminal_command_replays_after_restart(
    tmp_path: Path,
) -> None:
    now = datetime.now(timezone.utc)
    journal_path = tmp_path / "commands.json"
    first = _Actor(
        control_plane=_ControlPlane(),
        lifecycle=_Lifecycle(),
        message_bus=_MessageBus(),
        evidence_provider=_EvidenceProvider([_snapshot(now)]),
        journal_path=journal_path,
    )
    command = _command("cancel-restart", CommandType.CANCEL_ALL)
    first._pending_commands = (command,)
    assert first._apply_pending_commands() == 1
    first.on_stop()

    control_plane = _ControlPlane()
    message_bus = _MessageBus()
    restarted = _Actor(
        control_plane=control_plane,
        lifecycle=_Lifecycle(),
        message_bus=message_bus,
        evidence_provider=_EvidenceProvider([_snapshot(now), _snapshot(now)]),
        journal_path=journal_path,
    )

    restarted._replay_terminal_commands()

    assert len(message_bus.published) == 1
    replayed = restarted._command_journal.get(command.command_id)
    assert replayed
    assert replayed.phase == "running"
    assert replayed.result
    assert replayed.result["replayed_after_restart"] is True
    restarted.on_stop()


def _command(
    command_id: str,
    command_type: CommandType,
    *,
    authorized_by_type: str = "user",
) -> NodeCommand:
    authorization = dict(AUTHORIZATION)
    authorization["authorized_by_type"] = authorized_by_type
    return NodeCommand(
        command_id=command_id,
        type=command_type,
        args={
            "account_id": "account-a",
            "instrument_ids": ["SOLUSDT-PERP.BINANCE"],
            "authorization": authorization,
        },
    )


def _result_payload(
    command: NodeCommand,
    *,
    dispatched_at: datetime,
) -> dict[str, Any]:
    return {
        "command_id": command.command_id,
        "command_type": command.type.value,
        "account_id": "account-a",
        "instrument_ids": ["SOLUSDT-PERP.BINANCE"],
        "operations": [
            {
                "kind": "cancel_order",
                "symbol": "SOLUSDT",
                "status": "confirmed",
            }
        ],
        "errors": [],
        "dispatched_at": dispatched_at.isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


def _snapshot(
    fetched_at: datetime,
    *,
    positions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    position_rows = positions
    if position_rows is None:
        position_rows = []
    return {
        "positions": position_rows,
        "regular_orders": [],
        "algo_orders": [],
        "fetched_at": fetched_at,
    }


def _flush_ack(actor: CommandPollerActor) -> None:
    actor._submit_acks()
    assert _wait_until(lambda: actor._ack_future is not None)
    assert _wait_until(lambda: bool(actor._ack_future.done()))
    actor._harvest_acks()


def _wait_until(predicate: Any, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return bool(predicate())
