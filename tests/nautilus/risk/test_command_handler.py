from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Optional


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from commands.handler import (  # noqa: E402
    CommandAckStatus,
    CommandType,
    CommandProcessor,
    InMemoryCommandStateStore,
    NodeCommand,
    PositionSnapshot,
    TradingState,
)


class CommandProcessorTests(unittest.TestCase):
    def test_cancel_all_runs_while_halted_and_acks_completed(self) -> None:
        control_plane = _RecordingControlPlane()
        engine = _RecordingEngine()
        lifecycle = _RecordingLifecycle(TradingState.HALTED)
        processor = CommandProcessor(
            node_id="node-a",
            control_plane=control_plane,
            lifecycle=lifecycle,
            engine=engine,
            state_store=InMemoryCommandStateStore(),
        )
        command = NodeCommand(command_id="cmd-1", type=CommandType.CANCEL_ALL)

        processor.handle(command)

        self.assertEqual(engine.calls, [("cancel_all", None), ("wait_no_working_orders", None)])
        self.assertEqual(
            [(ack[1], ack[2]) for ack in control_plane.command_acks],
            [("cmd-1", CommandAckStatus.ACCEPTED), ("cmd-1", CommandAckStatus.COMPLETED)],
        )
        self.assertEqual(lifecycle.states, [])

    def test_close_all_reduces_then_cancels_then_closes_positions_before_ack(self) -> None:
        control_plane = _RecordingControlPlane()
        engine = _RecordingEngine(
            positions=[
                PositionSnapshot(position_id="pos-1", instrument_id="BTCUSDT-PERP.BINANCE"),
                PositionSnapshot(position_id="pos-2", instrument_id="ETHUSDT-PERP.BINANCE"),
            ]
        )
        lifecycle = _RecordingLifecycle(TradingState.ACTIVE)
        processor = CommandProcessor(
            node_id="node-a",
            control_plane=control_plane,
            lifecycle=lifecycle,
            engine=engine,
            state_store=InMemoryCommandStateStore(),
        )
        command = NodeCommand(command_id="cmd-2", type=CommandType.CLOSE_ALL)

        processor.handle(command)

        self.assertEqual(lifecycle.states, [(TradingState.REDUCING, "command cmd-2 close_all")])
        self.assertEqual(
            engine.calls,
            [
                ("cancel_all", None),
                ("wait_no_working_orders", None),
                ("open_positions", None),
                ("close_position_reduce_only", "pos-1"),
                ("close_position_reduce_only", "pos-2"),
                ("wait_flat", None),
            ],
        )
        self.assertEqual(
            [(ack[1], ack[2]) for ack in control_plane.command_acks],
            [("cmd-2", CommandAckStatus.ACCEPTED), ("cmd-2", CommandAckStatus.COMPLETED)],
        )

    def test_command_id_is_idempotent_and_does_not_reexecute_engine_calls(self) -> None:
        control_plane = _RecordingControlPlane()
        engine = _RecordingEngine()
        processor = CommandProcessor(
            node_id="node-a",
            control_plane=control_plane,
            lifecycle=_RecordingLifecycle(TradingState.HALTED),
            engine=engine,
            state_store=InMemoryCommandStateStore(),
        )
        command = NodeCommand(command_id="cmd-3", type=CommandType.CANCEL_ALL)

        processor.handle(command)
        processor.handle(command)

        self.assertEqual(engine.calls, [("cancel_all", None), ("wait_no_working_orders", None)])
        self.assertEqual(
            [(ack[1], ack[2]) for ack in control_plane.command_acks],
            [
                ("cmd-3", CommandAckStatus.ACCEPTED),
                ("cmd-3", CommandAckStatus.COMPLETED),
                ("cmd-3", CommandAckStatus.COMPLETED),
            ],
        )

    def test_halt_and_resume_drive_lifecycle_state(self) -> None:
        control_plane = _RecordingControlPlane()
        lifecycle = _RecordingLifecycle(TradingState.ACTIVE)
        processor = CommandProcessor(
            node_id="node-a",
            control_plane=control_plane,
            lifecycle=lifecycle,
            engine=_RecordingEngine(),
            state_store=InMemoryCommandStateStore(),
        )

        processor.handle(NodeCommand(command_id="cmd-4", type=CommandType.HALT, args={"reason": "operator"}))
        processor.handle(NodeCommand(command_id="cmd-5", type=CommandType.RESUME))

        self.assertEqual(
            lifecycle.states,
            [
                (TradingState.HALTED, "operator"),
                (TradingState.ACTIVE, "command cmd-5 resume"),
            ],
        )
        self.assertEqual(
            [(ack[1], ack[2]) for ack in control_plane.command_acks],
            [
                ("cmd-4", CommandAckStatus.ACCEPTED),
                ("cmd-4", CommandAckStatus.COMPLETED),
                ("cmd-5", CommandAckStatus.ACCEPTED),
                ("cmd-5", CommandAckStatus.COMPLETED),
            ],
        )

    def test_set_reducing_drives_lifecycle_state(self) -> None:
        control_plane = _RecordingControlPlane()
        lifecycle = _RecordingLifecycle(TradingState.ACTIVE)
        processor = CommandProcessor(
            node_id="node-a",
            control_plane=control_plane,
            lifecycle=lifecycle,
            engine=_RecordingEngine(),
            state_store=InMemoryCommandStateStore(),
        )

        processor.handle(NodeCommand(command_id="cmd-6", type=CommandType.SET_REDUCING))

        self.assertEqual(
            lifecycle.states,
            [(TradingState.REDUCING, "command cmd-6 set_reducing")],
        )
        self.assertEqual(
            [(ack[1], ack[2]) for ack in control_plane.command_acks],
            [
                ("cmd-6", CommandAckStatus.ACCEPTED),
                ("cmd-6", CommandAckStatus.COMPLETED),
            ],
        )

    def test_refresh_evidence_command_is_non_stateful_noop(self) -> None:
        control_plane = _RecordingControlPlane()
        lifecycle = _RecordingLifecycle(TradingState.HALTED)
        engine = _RecordingEngine()
        processor = CommandProcessor(
            node_id="node-a",
            control_plane=control_plane,
            lifecycle=lifecycle,
            engine=engine,
            state_store=InMemoryCommandStateStore(),
        )

        result = processor.handle(
            NodeCommand(
                command_id="cmd-refresh",
                type=CommandType.REFRESH_EVIDENCE,
            )
        )

        self.assertEqual(result.status, CommandAckStatus.COMPLETED)
        self.assertEqual(result.result, {"evidence_refreshed": True})
        self.assertEqual(lifecycle.states, [])
        self.assertEqual(engine.calls, [])

    def test_failed_command_id_is_idempotent_and_does_not_reexecute_engine_calls(self) -> None:
        control_plane = _RecordingControlPlane()
        engine = _RecordingEngine(fail_cancel=True)
        processor = CommandProcessor(
            node_id="node-a",
            control_plane=control_plane,
            lifecycle=_RecordingLifecycle(TradingState.HALTED),
            engine=engine,
            state_store=InMemoryCommandStateStore(),
        )
        command = NodeCommand(command_id="cmd-7", type=CommandType.CANCEL_ALL)

        processor.handle(command)
        processor.handle(command)

        self.assertEqual(engine.calls, [("cancel_all", None)])
        self.assertEqual(
            [(ack[1], ack[2]) for ack in control_plane.command_acks],
            [
                ("cmd-7", CommandAckStatus.ACCEPTED),
                ("cmd-7", CommandAckStatus.FAILED),
                ("cmd-7", CommandAckStatus.FAILED),
            ],
        )


class _RecordingControlPlane:
    def __init__(self) -> None:
        self.command_acks: list[
            tuple[str, str, CommandAckStatus, Optional[dict[str, Any]], Optional[str]]
        ] = []

    def ack_command(
        self,
        node_id: str,
        command_id: str,
        status: CommandAckStatus,
        result: Optional[dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        self.command_acks.append((node_id, command_id, status, result, error))


class _RecordingLifecycle:
    def __init__(self, state: TradingState) -> None:
        self.trading_state = state
        self.states: list[tuple[TradingState, str]] = []

    def apply_operator_state(self, state: TradingState, reason: str) -> None:
        self.trading_state = state
        self.states.append((state, reason))


class _RecordingEngine:
    def __init__(
        self,
        positions: Optional[list[PositionSnapshot]] = None,
        fail_cancel: bool = False,
    ) -> None:
        self._positions = positions or []
        self._fail_cancel = fail_cancel
        self.calls: list[tuple[str, Optional[str]]] = []

    def cancel_all(self, instrument_ids: Optional[tuple[str, ...]] = None) -> dict[str, Any]:
        self.calls.append(("cancel_all", None if instrument_ids is None else ",".join(instrument_ids)))
        if self._fail_cancel:
            raise RuntimeError("cancel failed")
        return {"cancel_requested": True}

    def wait_for_no_working_orders(self) -> None:
        self.calls.append(("wait_no_working_orders", None))

    def open_positions(self, instrument_ids: Optional[tuple[str, ...]] = None) -> list[PositionSnapshot]:
        self.calls.append(("open_positions", None if instrument_ids is None else ",".join(instrument_ids)))
        return list(self._positions)

    def close_position_reduce_only(self, position: PositionSnapshot) -> dict[str, Any]:
        self.calls.append(("close_position_reduce_only", position.position_id))
        return {"position_id": position.position_id}

    def wait_until_flat(self, positions: list[PositionSnapshot]) -> None:
        del positions
        self.calls.append(("wait_flat", None))


if __name__ == "__main__":
    unittest.main()
