from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import UUID

from data_client.atomic_json import write_json_atomic
from execution_domain.contracts import ApprovedTradeIntentV1


@dataclass(frozen=True)
class DurableIntentReceipt:
    cursor: str
    intent: ApprovedTradeIntentV1
    status: str
    detail: str


class JsonDurableIntentInbox:
    """Restart-persistent receipt store for accepted intent delivery."""

    _SCHEMA_VERSION = 3
    _INTERMEDIATE_STATUSES = frozenset({
        "RECEIVED",
        "PREPARED",
        "DISPATCHED",
        "RESUBMITTING",
    })
    _TERMINAL_STATUSES = frozenset({
        "CONFIRMED",
        "EXCHANGE_CONFIRMED",
        "REJECTED",
        "DENIED",
        "CANCELED",
        "EXPIRED",
        "FILLED",
    })
    _INTERMEDIATE_STATUS_RANK = {
        "RECEIVED": 1,
        "PREPARED": 2,
        "DISPATCHED": 3,
        "RESUBMITTING": 4,
    }

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = RLock()
        self._records = self._load_records()

    @property
    def path(self) -> Path:
        return self._path

    def receive(
        self,
        cursor: str,
        intent: ApprovedTradeIntentV1,
    ) -> None:
        with self._lock:
            key = str(intent.intent_id)
            existing = self._records.get(key)
            if existing is not None:
                if str(existing["cursor"]) != str(cursor):
                    raise ValueError(
                        "durable intent inbox cursor mismatch"
                    )
                return
            records = dict(self._records)
            records[key] = {
                "cursor": str(cursor),
                "intent": intent.model_dump(mode="json"),
                "status": "RECEIVED",
                "detail": "",
            }
            self._write_records(records)
            self._records = records

    def transition(
        self,
        intent_id: UUID | str,
        *,
        expected_status: str,
        status: str,
        detail: str,
    ) -> bool:
        with self._lock:
            key = str(intent_id)
            record = self._records.get(key)
            if record is None:
                return False
            normalized_expected = self._validated_status(
                expected_status
            )
            current_status = self._validated_status(
                record["status"]
            )
            if current_status != normalized_expected:
                return False
            normalized_status = self._validated_status(status)
            records = dict(self._records)
            updated = dict(record)
            updated["status"] = normalized_status
            updated["detail"] = str(detail)
            records[key] = updated
            self._write_records(records)
            self._records = records
            return True

    def complete(
        self,
        intent_id: UUID | str,
        status: str,
        detail: str,
    ) -> None:
        with self._lock:
            key = str(intent_id)
            if key not in self._records:
                return
            normalized_status = str(status).strip().upper()
            normalized_detail = str(detail)
            records = dict(self._records)
            if normalized_status in self._TERMINAL_STATUSES:
                records.pop(key, None)
            else:
                if normalized_status not in self._INTERMEDIATE_STATUSES:
                    raise ValueError(
                        "unsupported durable intent receipt status: "
                        f"{normalized_status}"
                    )
                record = dict(records[key])
                current_status = self._validated_status(
                    record["status"]
                )
                if (
                    self._INTERMEDIATE_STATUS_RANK[
                        normalized_status
                    ]
                    < self._INTERMEDIATE_STATUS_RANK[
                        current_status
                    ]
                ):
                    return
                record["status"] = normalized_status
                record["detail"] = normalized_detail
                records[key] = record
            self._write_records(records)
            self._records = records

    def get(
        self,
        intent_id: UUID | str,
    ) -> DurableIntentReceipt | None:
        with self._lock:
            record = self._records.get(str(intent_id))
            if record is None:
                return None
            return self._receipt(record)

    def status(
        self,
        intent_id: UUID | str,
    ) -> str | bool:
        with self._lock:
            record = self._records.get(str(intent_id))
            if record is None:
                return False
            return self._validated_status(record["status"])

    def pending(self) -> tuple[DurableIntentReceipt, ...]:
        with self._lock:
            records = sorted(
                self._records.values(),
                key=lambda record: str(record["cursor"]),
            )
            return tuple(
                self._receipt(record) for record in records
            )

    def _load_records(self) -> dict[str, dict[str, Any]]:
        if not self._path.exists():
            return {}
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise TypeError("durable intent inbox must be an object")
        schema_version = raw.get("schema_version")
        if schema_version not in {1, 2, self._SCHEMA_VERSION}:
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
                "status": self._validated_status(
                    record.get("status", "RECEIVED")
                ),
                "detail": str(record.get("detail", "")),
            }
        return validated

    def _receipt(
        self,
        record: dict[str, Any],
    ) -> DurableIntentReceipt:
        return DurableIntentReceipt(
            cursor=str(record["cursor"]),
            intent=ApprovedTradeIntentV1.model_validate(
                record["intent"]
            ),
            status=self._validated_status(record["status"]),
            detail=str(record.get("detail", "")),
        )

    def _validated_status(self, status: Any) -> str:
        normalized = str(status).strip().upper()
        if normalized not in self._INTERMEDIATE_STATUSES:
            raise ValueError(
                "durable intent inbox contains terminal receipt"
            )
        return normalized

    def _write_records(
        self,
        records: dict[str, Any],
    ) -> None:
        payload = {
            "schema_version": self._SCHEMA_VERSION,
            "records": records,
        }
        write_json_atomic(self._path, payload)
