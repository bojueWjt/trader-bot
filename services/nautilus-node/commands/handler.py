from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Protocol

try:
    from execution_domain.control_plane import (
        CommandAckStatus,
        CommandType,
        NodeCommand,
        NodeCommandChannel,
        TradingState,
    )
except ModuleNotFoundError as exc:
    if exc.name != "pydantic":
        raise
    # Local pudu-mini unittest may not have pydantic, which is imported by the
    # execution_domain package. Keep the fallback value-compatible with the seam;
    # production imports the real classes.
    class TradingState(str, Enum):
        ACTIVE = "ACTIVE"
        HALTED = "HALTED"
        REDUCING = "REDUCING"

    class CommandType(str, Enum):
        HALT = "halt"
        RESUME = "resume"
        SET_REDUCING = "set_reducing"
        CANCEL_ALL = "cancel_all"
        CLOSE_ALL = "close_all"

    class CommandAckStatus(str, Enum):
        ACCEPTED = "accepted"
        COMPLETED = "completed"
        FAILED = "failed"

    @dataclass(frozen=True)
    class NodeCommand:
        command_id: str
        type: CommandType
        args: dict[str, Any] = field(default_factory=dict)
        issued_at: Any = None

    class NodeCommandChannel(Protocol):
        def ack_command(
            self,
            node_id: str,
            command_id: str,
            status: CommandAckStatus,
            result: Optional[dict[str, Any]] = None,
            error: Optional[str] = None,
        ) -> None: ...


@dataclass(frozen=True)
class PositionSnapshot:
    position_id: str
    instrument_id: str


@dataclass(frozen=True)
class CommandStateRecord:
    command_id: str
    command_type: str
    status: CommandAckStatus
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None


class CommandStateStore(Protocol):
    def get(self, command_id: str) -> Optional[CommandStateRecord]: ...

    def save(self, record: CommandStateRecord) -> None: ...


class LifecycleGate(Protocol):
    @property
    def trading_state(self) -> TradingState: ...

    def apply_operator_state(self, state: TradingState, reason: str) -> None: ...


class CommandExecutionEngine(Protocol):
    def cancel_all(self, instrument_ids: Optional[tuple[str, ...]] = None) -> dict[str, Any]: ...

    def wait_for_no_working_orders(self) -> None: ...

    def open_positions(self, instrument_ids: Optional[tuple[str, ...]] = None) -> list[PositionSnapshot]: ...

    def close_position_reduce_only(self, position: PositionSnapshot) -> dict[str, Any]: ...

    def wait_until_flat(self, positions: list[PositionSnapshot]) -> None: ...


class InMemoryCommandStateStore(CommandStateStore):
    def __init__(self) -> None:
        self._records: dict[str, CommandStateRecord] = {}

    def get(self, command_id: str) -> Optional[CommandStateRecord]:
        return self._records.get(command_id)

    def save(self, record: CommandStateRecord) -> None:
        self._records[record.command_id] = record


class JsonCommandStateStore(CommandStateStore):
    """Small durable idempotency store for completed command ids."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    def get(self, command_id: str) -> Optional[CommandStateRecord]:
        raw = self._load().get(command_id)
        if raw is None:
            return None
        return CommandStateRecord(
            command_id=str(raw["command_id"]),
            command_type=str(raw["command_type"]),
            status=CommandAckStatus(raw["status"]),
            result=raw.get("result"),
            error=raw.get("error"),
        )

    def save(self, record: CommandStateRecord) -> None:
        data = self._load()
        payload = asdict(record)
        payload["status"] = record.status.value
        data[record.command_id] = payload
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self._path)

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        return json.loads(self._path.read_text(encoding="utf-8"))


class CommandProcessor:
    def __init__(
        self,
        node_id: str,
        control_plane: NodeCommandChannel,
        lifecycle: LifecycleGate,
        engine: CommandExecutionEngine,
        state_store: CommandStateStore,
    ) -> None:
        self._node_id = node_id
        self._control_plane = control_plane
        self._lifecycle = lifecycle
        self._engine = engine
        self._state_store = state_store

    def handle(self, command: NodeCommand) -> CommandStateRecord:
        existing = self._state_store.get(command.command_id)
        if existing is not None:
            self._ack(command.command_id, existing.status, existing.result, existing.error)
            return existing

        self._ack(command.command_id, CommandAckStatus.ACCEPTED)
        accepted = CommandStateRecord(
            command_id=command.command_id,
            command_type=command.type.value,
            status=CommandAckStatus.ACCEPTED,
        )
        self._state_store.save(accepted)

        try:
            result = self._execute(command)
        except Exception as exc:
            failed = CommandStateRecord(
                command_id=command.command_id,
                command_type=command.type.value,
                status=CommandAckStatus.FAILED,
                error=str(exc),
            )
            self._state_store.save(failed)
            self._ack(command.command_id, CommandAckStatus.FAILED, error=str(exc))
            return failed

        completed = CommandStateRecord(
            command_id=command.command_id,
            command_type=command.type.value,
            status=CommandAckStatus.COMPLETED,
            result=result,
        )
        self._state_store.save(completed)
        self._ack(command.command_id, CommandAckStatus.COMPLETED, result=result)
        return completed

    def _execute(self, command: NodeCommand) -> dict[str, Any]:
        if command.type == CommandType.HALT:
            reason = str(command.args.get("reason") or f"command {command.command_id} halt")
            self._lifecycle.apply_operator_state(TradingState.HALTED, reason=reason)
            return {"trading_state": TradingState.HALTED.value}

        if command.type == CommandType.RESUME:
            self._lifecycle.apply_operator_state(
                TradingState.ACTIVE,
                reason=f"command {command.command_id} resume",
            )
            return {"trading_state": TradingState.ACTIVE.value}

        if command.type == CommandType.SET_REDUCING:
            self._lifecycle.apply_operator_state(
                TradingState.REDUCING,
                reason=f"command {command.command_id} set_reducing",
            )
            return {"trading_state": TradingState.REDUCING.value}

        if command.type == CommandType.CANCEL_ALL:
            return self._cancel_all(command)

        if command.type == CommandType.CLOSE_ALL:
            return self._close_all(command)

        raise ValueError(f"unsupported command type {command.type!r}")

    def _cancel_all(self, command: NodeCommand) -> dict[str, Any]:
        instrument_ids = _instrument_ids(command.args)
        cancel_result = self._engine.cancel_all(instrument_ids)
        self._engine.wait_for_no_working_orders()
        return {
            "trading_state": self._lifecycle.trading_state.value,
            "cancel": cancel_result,
        }

    def _close_all(self, command: NodeCommand) -> dict[str, Any]:
        instrument_ids = _instrument_ids(command.args)
        self._lifecycle.apply_operator_state(
            TradingState.REDUCING,
            reason=f"command {command.command_id} close_all",
        )
        cancel_result = self._engine.cancel_all(instrument_ids)
        self._engine.wait_for_no_working_orders()
        positions = self._engine.open_positions(instrument_ids)
        close_results = [
            self._engine.close_position_reduce_only(position)
            for position in positions
        ]
        self._engine.wait_until_flat(positions)
        return {
            "trading_state": TradingState.REDUCING.value,
            "cancel": cancel_result,
            "closed_positions": close_results,
        }

    def _ack(
        self,
        command_id: str,
        status: CommandAckStatus,
        result: Optional[dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        self._control_plane.ack_command(
            self._node_id,
            command_id,
            status,
            result=result,
            error=error,
        )


def _instrument_ids(args: dict[str, Any]) -> Optional[tuple[str, ...]]:
    raw = args.get("instrument_ids")
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw):
        raise ValueError("instrument_ids must be a list of non-empty strings")
    return tuple(raw)
