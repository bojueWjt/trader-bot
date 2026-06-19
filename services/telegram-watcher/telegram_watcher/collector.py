from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


class IngressClient(Protocol):
    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        ...


class ObjectStore(Protocol):
    def put_bytes(
        self,
        *,
        source: str,
        channel_id: str,
        source_message_id: str,
        data: bytes,
        filename: str | None,
        mime: str | None,
        width: int | None,
        height: int | None,
    ) -> dict[str, Any]:
        ...


class TelegramWatcherAdapter:
    def __init__(
        self,
        *,
        ingress_client: IngressClient,
        object_store: ObjectStore,
        delivery_log_path: Path,
        max_attempts: int = 3,
        retry_sleep_seconds: float = 0.05,
    ):
        self.ingress_client = ingress_client
        self.object_store = object_store
        self.delivery_log_path = Path(delivery_log_path)
        self.max_attempts = max_attempts
        self.retry_sleep_seconds = retry_sleep_seconds

    def handle_update(self, update: Any) -> dict[str, Any]:
        payload = self._payload_from_update(update)
        last_error: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                result = self.ingress_client.submit(payload)
                self._record_delivery(payload, result=result, attempt=attempt)
                return result
            except Exception as exc:
                last_error = exc
                self._record_delivery(payload, error=str(exc), attempt=attempt)
                if attempt < self.max_attempts:
                    time.sleep(self.retry_sleep_seconds)

        raise RuntimeError(f"ingress delivery failed after {self.max_attempts} attempts") from last_error

    def _payload_from_update(self, update: Any) -> dict[str, Any]:
        message = _get(update, "message", update)
        channel_id = str(_required(message, "chat_id"))
        source_message_id = str(_required(message, "id"))
        source_received_at = _isoformat(_required(message, "date"))
        edit_date = _get(message, "edit_date")
        source_version = str(_get(message, "source_version") or (f"edit:{_isoformat(edit_date)}" if edit_date else "v1"))
        text = _get(message, "text")
        media_assets = self._store_media(message, channel_id, source_message_id)
        reply_to_id = _get(message, "reply_to_msg_id")
        reply_to = {"source_message_id": str(reply_to_id), "source_version": "v1"} if reply_to_id else None

        return {
            "source": "telegram",
            "channel_id": channel_id,
            "source_message_id": source_message_id,
            "source_version": source_version,
            "source_received_at": source_received_at,
            "author_id": _optional_string(_get(message, "sender_id")),
            "message_text": _optional_string(text),
            "message_kind": _message_kind(message, media_assets, edit_date),
            "update_id": _optional_string(_get(update, "update_id")),
            "reply_to": reply_to,
            "media_assets": media_assets,
            "raw_payload": _json_safe(update),
        }

    def _store_media(self, message: Any, channel_id: str, source_message_id: str) -> list[dict[str, Any]]:
        media_items = _get(message, "media", [])
        if media_items is None:
            return []
        if isinstance(media_items, dict):
            media_items = [media_items]
        if not isinstance(media_items, list):
            raise ValueError("message media must be an object or list")

        assets: list[dict[str, Any]] = []
        for item in media_items:
            data = _get(item, "bytes")
            if data is None:
                continue
            if not isinstance(data, bytes):
                raise ValueError("media bytes must be bytes")
            assets.append(
                self.object_store.put_bytes(
                    source="telegram",
                    channel_id=channel_id,
                    source_message_id=source_message_id,
                    data=data,
                    filename=_optional_string(_get(item, "filename")),
                    mime=_optional_string(_get(item, "mime")),
                    width=_optional_int(_get(item, "width")),
                    height=_optional_int(_get(item, "height")),
                )
            )
        return assets

    def _record_delivery(
        self,
        payload: dict[str, Any],
        *,
        attempt: int,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        self.delivery_log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "attempt": attempt,
            "source": payload["source"],
            "channel_id": payload["channel_id"],
            "source_message_id": payload["source_message_id"],
            "source_version": payload["source_version"],
            "update_id": payload.get("update_id"),
            "result": result,
            "error": error,
        }
        with self.delivery_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _required(value: Any, key: str) -> Any:
    current = _get(value, key)
    if current is None or current == "":
        raise ValueError(f"telegram message {key} is required")
    return current


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _isoformat(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _message_kind(message: Any, media_assets: list[dict[str, Any]], edit_date: Any) -> str:
    if edit_date:
        return "edit"
    if media_assets:
        first_mime = media_assets[0].get("mime") or ""
        return "photo" if first_mime.startswith("image/") else "media"
    return "text"


def _json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"byte_count": len(value)}
    if isinstance(value, datetime):
        return _isoformat(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
