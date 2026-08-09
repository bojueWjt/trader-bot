from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from execution_domain.contracts import ApprovedTradeIntentV1


@dataclass(frozen=True)
class DurableIntentReceipt:
    cursor: str
    intent: ApprovedTradeIntentV1


class JsonDurableIntentInbox:
    """Restart-persistent receipt store for accepted intent delivery."""

    _SCHEMA_VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._records = self._load_records()

    @property
    def path(self) -> Path:
        return self._path

    def receive(
        self,
        cursor: str,
        intent: ApprovedTradeIntentV1,
    ) -> None:
        records = dict(self._records)
        records[str(intent.intent_id)] = {
            "cursor": str(cursor),
            "intent": intent.model_dump(mode="json"),
        }
        self._write_records(records)
        self._records = records

    def complete(
        self,
        intent_id: UUID | str,
        status: str,
        detail: str,
    ) -> None:
        del status, detail
        key = str(intent_id)
        if key not in self._records:
            return
        records = dict(self._records)
        records.pop(key, None)
        self._write_records(records)
        self._records = records

    def pending(self) -> tuple[DurableIntentReceipt, ...]:
        pending: list[DurableIntentReceipt] = []
        for key in sorted(self._records):
            record = self._records[key]
            pending.append(
                DurableIntentReceipt(
                    cursor=str(record["cursor"]),
                    intent=ApprovedTradeIntentV1.model_validate(
                        record["intent"]
                    ),
                )
            )
        return tuple(pending)

    def _load_records(self) -> dict[str, dict[str, Any]]:
        if not self._path.exists():
            return {}
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise TypeError("durable intent inbox must be an object")
        if raw.get("schema_version") != self._SCHEMA_VERSION:
            raise ValueError(
                "durable intent inbox schema version mismatch"
            )
        records = raw.get("records")
        if not isinstance(records, dict):
            raise TypeError(
                "durable intent inbox records must be an object"
            )
        validated: dict[str, dict[str, Any]] = {}
        for key, record in records.items():
            if not isinstance(record, dict):
                raise TypeError(
                    "durable intent inbox record must be an object"
                )
            cursor = record.get("cursor")
            payload = record.get("intent")
            if not isinstance(cursor, str) or not cursor:
                raise ValueError(
                    "durable intent inbox cursor is required"
                )
            intent = ApprovedTradeIntentV1.model_validate(payload)
            if str(intent.intent_id) != str(key):
                raise ValueError(
                    "durable intent inbox intent id mismatch"
                )
            validated[str(key)] = {
                "cursor": cursor,
                "intent": intent.model_dump(mode="json"),
            }
        return validated

    def _write_records(
        self,
        records: dict[str, Any],
    ) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": self._SCHEMA_VERSION,
            "records": records,
        }
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            dir=str(self._path.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                json.dump(
                    payload,
                    tmp,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                tmp.write("\n")
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, self._path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
