from __future__ import annotations

import json
import time
from typing import Any, Callable, Iterable


Sleep = Callable[[float], None]


class CommandResultSink:
    def __init__(
        self,
        control_plane: Any,
        *,
        node_id: str,
        max_ack_attempts: int = 3,
        retry_delay_seconds: float = 0.05,
        sleep: Sleep = time.sleep,
    ) -> None:
        self._control_plane = control_plane
        self._node_id = node_id
        self._max_ack_attempts = max(max_ack_attempts, 1)
        self._retry_delay_seconds = retry_delay_seconds
        self._sleep = sleep
        self._results: dict[str, dict[str, Any]] = {}
        self._acked: set[str] = set()

    def record_running(
        self,
        command_id: str,
        *,
        result: dict[str, Any] | None = None,
        items: Iterable[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return self.record(command_id, "running", result=result, items=items)

    def record_completed(
        self,
        command_id: str,
        *,
        result: dict[str, Any] | None = None,
        items: Iterable[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return self.record(command_id, "completed", result=result, items=items)

    def record_failed(
        self,
        command_id: str,
        *,
        error: str,
        result: dict[str, Any] | None = None,
        items: Iterable[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return self.record(command_id, "failed", result=result, error=error, items=items)

    def record(
        self,
        command_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        items: Iterable[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload = self._build_result(command_id, status, result=result, error=error, items=items)
        ack_key = self._ack_key(command_id, status, result, error)
        if ack_key not in self._acked:
            self._ack(command_id, status, result=result, error=error)
            self._acked.add(ack_key)
        self._results[command_id] = payload
        return payload

    def get_result(self, command_id: str) -> dict[str, Any] | None:
        result = self._results.get(command_id)
        return dict(result) if result is not None else None

    def get_item_result(self, command_id: str, item_id: str) -> dict[str, Any] | None:
        result = self._results.get(command_id)
        if result is None:
            return None
        item = result.get("items", {}).get(item_id)
        return dict(item) if item is not None else None

    def _build_result(
        self,
        command_id: str,
        status: str,
        *,
        result: dict[str, Any] | None,
        error: str | None,
        items: Iterable[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        item_map = dict(self._results.get(command_id, {}).get("items", {}))
        for item in items or ():
            item_id = _item_id(item)
            item_map[item_id] = dict(item)
        return {
            "command_id": command_id,
            "status": status,
            "result": dict(result or {}),
            "error": error,
            "items": item_map,
        }

    def _ack(
        self,
        command_id: str,
        status: str,
        *,
        result: dict[str, Any] | None,
        error: str | None,
    ) -> None:
        last_error: Exception | None = None
        for attempt in range(1, self._max_ack_attempts + 1):
            try:
                self._control_plane.ack_command(
                    self._node_id,
                    command_id,
                    status,
                    result=result,
                    error=error,
                )
                return
            except Exception as exc:
                last_error = exc
                if attempt < self._max_ack_attempts:
                    self._sleep(self._retry_delay_seconds)
        if last_error is not None:
            raise last_error

    @staticmethod
    def _ack_key(
        command_id: str,
        status: str,
        result: dict[str, Any] | None,
        error: str | None,
    ) -> str:
        return json.dumps(
            {
                "command_id": command_id,
                "status": status,
                "result": result or {},
                "error": error,
            },
            sort_keys=True,
            default=str,
        )


def _item_id(item: dict[str, Any]) -> str:
    for field in ("item_id", "order_id", "position_key", "position_id", "client_order_id"):
        value = item.get(field)
        if value:
            return str(value)
    raise ValueError("command item result missing stable item id")
