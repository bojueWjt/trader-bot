from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ExecutionStateStore:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._state = self._load()

    def save_cursor(self, name: str, value: str) -> None:
        self._state.setdefault("cursors", {})[name] = value
        self._flush()

    def load_cursor(self, name: str) -> str | None:
        return self._state.get("cursors", {}).get(name)

    def remember_accepted_order(self, execution_job_id: str, client_order_id: str) -> None:
        self._state.setdefault("accepted_orders", {})[execution_job_id] = client_order_id
        self._flush()

    def was_order_accepted(self, execution_job_id: str, client_order_id: str) -> bool:
        return self._state.get("accepted_orders", {}).get(execution_job_id) == client_order_id

    def spool_job(self, job: dict[str, Any]) -> None:
        key = str(job.get("execution_job_id"))
        spool = self._state.setdefault("spool", [])
        if not any(str(item.get("execution_job_id")) == key for item in spool):
            spool.append(job)
            self._flush()

    def load_spool(self) -> list[dict[str, Any]]:
        return list(self._state.get("spool", []))

    def clear_spool(self) -> None:
        self._state["spool"] = []
        self._flush()

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"cursors": {}, "accepted_orders": {}, "spool": []}
        with self._path.open("r", encoding="utf-8") as fh:
            state = json.load(fh)
        state.setdefault("cursors", {})
        state.setdefault("accepted_orders", {})
        state.setdefault("spool", [])
        return state

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self._state, fh, sort_keys=True)
        tmp.replace(self._path)
