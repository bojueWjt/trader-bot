from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from psycopg2 import errors


CONTROL_PLANE = Path(__file__).resolve().parents[2] / "control-plane"
if str(CONTROL_PLANE) not in sys.path:
    sys.path.insert(0, str(CONTROL_PLANE))

from db.connection import connect, transaction  # noqa: E402
from db.repository import ingest_raw_message_with_outbox  # noqa: E402


SOURCE = "telegram"
OUTBOX_EVENT_TYPE = "queued_for_hermes"
DOWNLOAD_STATUSES = {"pending", "downloaded", "failed", "skipped"}
HEX_DIGITS = set("0123456789abcdefABCDEF")


class IngressValidationError(ValueError):
    pass


def ingest_raw_telegram_update(payload: dict[str, Any], database_url: str | None = None) -> dict[str, Any]:
    request = _normalize_payload(payload)
    conn = connect(database_url)
    try:
        try:
            with transaction(conn):
                existing_id = _find_existing_raw_message_id(conn, request)
                if existing_id is not None:
                    return _duplicate_result(existing_id)

                raw_message_id = uuid4()
                outbox_event_id = uuid4()
                raw_payload = _raw_payload(request)
                content_hash = _content_hash(raw_payload, request["message_text"], request["media_assets"])

                inserted_raw_id, inserted_outbox_id = ingest_raw_message_with_outbox(
                    conn,
                    {
                        "id": raw_message_id,
                        "source": SOURCE,
                        "channel_id": request["channel_id"],
                        "source_message_id": request["source_message_id"],
                        "source_version": request["source_version"],
                        "source_received_at": request["source_received_at"],
                        "author_id": request.get("author_id"),
                        "content_hash": content_hash,
                        "message_text": request.get("message_text"),
                        "raw_payload": raw_payload,
                    },
                    {
                        "outbox_event_id": outbox_event_id,
                        "aggregate_type": "raw_message",
                        "aggregate_id": raw_message_id,
                        "event_type": OUTBOX_EVENT_TYPE,
                        "payload": {
                            "raw_message_id": str(raw_message_id),
                            "source": SOURCE,
                            "channel_id": request["channel_id"],
                            "source_message_id": request["source_message_id"],
                            "source_version": request["source_version"],
                        },
                        "trace_id": raw_message_id,
                    },
                )
                _insert_media_assets(conn, inserted_raw_id, request["media_assets"])
                return {
                    "inserted": True,
                    "raw_message_id": str(inserted_raw_id),
                    "outbox_event_id": str(inserted_outbox_id),
                    "source_version": request["source_version"],
                }
        except errors.UniqueViolation:
            conn.rollback()
            existing_id = _find_existing_raw_message_id(conn, request)
            if existing_id is None:
                raise
            return _duplicate_result(existing_id)
    finally:
        conn.close()


def _normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise IngressValidationError("payload must be an object")

    source = payload.get("source", SOURCE)
    if source != SOURCE:
        raise IngressValidationError("only telegram source is accepted")

    request = {
        "source": SOURCE,
        "channel_id": _required_text(payload, "channel_id"),
        "source_message_id": _required_text(payload, "source_message_id"),
        "source_version": _required_text(payload, "source_version"),
        "source_received_at": _required_text(payload, "source_received_at"),
        "author_id": _optional_text(payload, "author_id"),
        "message_text": _optional_text(payload, "message_text"),
        "message_kind": _required_text(payload, "message_kind"),
        "update_id": _optional_text(payload, "update_id"),
        "reply_to": payload.get("reply_to"),
        "raw_payload": payload.get("raw_payload", {}),
        "media_assets": _normalize_media_assets(payload.get("media_assets", [])),
    }

    if not isinstance(request["raw_payload"], dict):
        raise IngressValidationError("raw_payload must be an object")
    if request["reply_to"] is not None and not isinstance(request["reply_to"], dict):
        raise IngressValidationError("reply_to must be an object when present")
    return request


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None or str(value) == "":
        raise IngressValidationError(f"{key} is required")
    return str(value)


def _optional_text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    return str(value)


def _normalize_media_assets(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise IngressValidationError("media_assets must be a list")

    assets: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise IngressValidationError("media asset must be an object")
        sha256 = _required_text(item, "sha256").lower()
        if not _is_sha256(sha256):
            raise IngressValidationError("media sha256 must be a 64 character hex digest")
        width = _positive_int_or_none(item.get("width"), "width")
        height = _positive_int_or_none(item.get("height"), "height")
        status = str(item.get("download_status", "pending"))
        if status not in DOWNLOAD_STATUSES:
            raise IngressValidationError("invalid media download_status")
        assets.append(
            {
                "sha256": sha256,
                "object_key": _optional_text(item, "object_key"),
                "mime": _optional_text(item, "mime"),
                "width": width,
                "height": height,
                "download_status": status,
                "downloaded_at": _optional_text(item, "downloaded_at"),
            }
        )
    return assets


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in HEX_DIGITS for char in value)


def _positive_int_or_none(value: Any, key: str) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise IngressValidationError(f"{key} must be an integer") from exc
    if parsed <= 0:
        raise IngressValidationError(f"{key} must be positive")
    return parsed


def _raw_payload(request: dict[str, Any]) -> dict[str, Any]:
    raw_payload = dict(request["raw_payload"])
    raw_payload["source"] = SOURCE
    raw_payload["update_id"] = request.get("update_id")
    raw_payload["message_kind"] = request["message_kind"]
    raw_payload["reply_to"] = request.get("reply_to")
    raw_payload["source_received_at"] = request["source_received_at"]
    raw_payload["media_assets"] = [
        {
            "sha256": asset["sha256"],
            "object_key": asset.get("object_key"),
            "mime": asset.get("mime"),
            "width": asset.get("width"),
            "height": asset.get("height"),
            "download_status": asset["download_status"],
        }
        for asset in request["media_assets"]
    ]
    return raw_payload


def _content_hash(raw_payload: dict[str, Any], message_text: str | None, media_assets: list[dict[str, Any]]) -> str:
    body = {
        "message_text": message_text,
        "media_assets": media_assets,
        "raw_payload": raw_payload,
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _find_existing_raw_message_id(conn, request: dict[str, Any]) -> UUID | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id
            FROM raw_messages
            WHERE source = %s
              AND channel_id = %s
              AND source_message_id = %s
              AND source_version = %s
            """,
            (
                SOURCE,
                request["channel_id"],
                request["source_message_id"],
                request["source_version"],
            ),
        )
        row = cur.fetchone()
    return UUID(row[0]) if row else None


def _insert_media_assets(conn, raw_message_id: UUID, media_assets: list[dict[str, Any]]) -> None:
    if not media_assets:
        return

    with conn.cursor() as cur:
        for asset in media_assets:
            cur.execute(
                """
                INSERT INTO media_assets (
                    asset_id, raw_message_id, sha256, object_key, mime,
                    width, height, download_status, downloaded_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (raw_message_id, sha256) DO NOTHING
                """,
                (
                    str(uuid4()),
                    str(raw_message_id),
                    asset["sha256"],
                    asset.get("object_key"),
                    asset.get("mime"),
                    asset.get("width"),
                    asset.get("height"),
                    asset["download_status"],
                    asset.get("downloaded_at"),
                ),
            )


def _duplicate_result(raw_message_id: UUID) -> dict[str, Any]:
    return {
        "inserted": False,
        "raw_message_id": str(raw_message_id),
        "outbox_event_id": None,
    }
