from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence
from uuid import UUID

from .contracts import ExecutionEventEnvelopeV1
from .control_plane import (
    CommandAckStatus,
    ControlPlaneClient,
    Heartbeat,
    IntentAckStatus,
    IntentBatch,
    IntentItem,
    NodeCommand,
)


class InMemoryControlPlane(ControlPlaneClient):
    """Deterministic control-plane test double implementing the seam Protocol."""

    def __init__(self, now: Optional[Callable[[], datetime]] = None) -> None:
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._intents: dict[str, list[IntentItem]] = defaultdict(list)
        self.intent_acks: list[tuple[str, str, UUID, IntentAckStatus, Optional[str]]] = []
        self.events_by_node: dict[str, dict[str, ExecutionEventEnvelopeV1]] = defaultdict(dict)
        self.heartbeats: dict[str, tuple[datetime, Heartbeat]] = {}
        self._commands_by_node: dict[str, list[NodeCommand]] = defaultdict(list)
        self.command_acks: list[
            tuple[str, str, CommandAckStatus, Optional[dict[str, object]], Optional[str]]
        ] = []
        self._snapshots_by_account: dict[str, datetime] = {}
        self._open_orders_by_account: dict[
            str,
            tuple[dict[str, Any], ...],
        ] = {}

    def add_intent(self, account_id: str, item: IntentItem) -> None:
        self._intents[account_id].append(item)

    def fetch_intents(
        self,
        account_id: str,
        after_cursor: Optional[str],
        limit: int,
        wait_ms: int = 0,
    ) -> IntentBatch:
        del wait_ms
        items = self._intents.get(account_id, [])
        start = 0
        if after_cursor is not None:
            for index, item in enumerate(items):
                if item.cursor == after_cursor:
                    start = index + 1
                    break
        page = items[start : start + limit]
        next_cursor = page[-1].cursor if page else after_cursor
        return IntentBatch(items=page, next_cursor=next_cursor)

    def ack_intent(
        self,
        account_id: str,
        node_id: str,
        intent_id: UUID,
        status: IntentAckStatus,
        detail: Optional[str] = None,
    ) -> None:
        self.intent_acks.append((account_id, node_id, intent_id, status, detail))

    def post_events(
        self, node_id: str, events: Sequence[ExecutionEventEnvelopeV1]
    ) -> list[str]:
        accepted: list[str] = []
        for event in events:
            self.events_by_node[node_id][event.event_id] = event
            accepted.append(event.event_id)
        return accepted

    def heartbeat(self, node_id: str, hb: Heartbeat) -> None:
        self.heartbeats[node_id] = (self._now(), hb)

    def poll_commands(self, node_id: str, after: Optional[str]) -> list[NodeCommand]:
        commands = self._commands_by_node.get(node_id, [])
        if after is None:
            return list(commands)
        for index, command in enumerate(commands):
            if command.command_id == after:
                return list(commands[index + 1 :])
        return list(commands)

    def add_command(self, node_id: str, command: NodeCommand) -> None:
        self._commands_by_node[node_id].append(command)

    def ack_command(
        self,
        node_id: str,
        command_id: str,
        status: CommandAckStatus,
        result: Optional[dict[str, object]] = None,
        error: Optional[str] = None,
    ) -> None:
        self.command_acks.append((node_id, command_id, status, result, error))

    def record_snapshot(self, account_id: str, generated_at: datetime) -> None:
        self._snapshots_by_account[account_id] = generated_at

    def latest_snapshot_generated_at(self, account_id: str) -> Optional[datetime]:
        return self._snapshots_by_account.get(account_id)

    def record_open_orders(
        self,
        account_id: str,
        orders: Sequence[Mapping[str, Any]],
    ) -> None:
        self._open_orders_by_account[account_id] = tuple(
            dict(order)
            for order in orders
        )

    def fetch_open_orders(
        self,
        account_id: str,
    ) -> tuple[dict[str, Any], ...]:
        return self._open_orders_by_account.get(account_id, ())
