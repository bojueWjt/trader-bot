from __future__ import annotations

import hashlib
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class LocalObjectStore:
    def __init__(self, root: Path):
        self.root = Path(root)

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
        sha256 = hashlib.sha256(data).hexdigest()
        extension = _extension(filename, mime)
        object_key = f"{source}/{channel_id}/{source_message_id}/{sha256}{extension}"
        destination = self.root / object_key
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            destination.write_bytes(data)
        return {
            "sha256": sha256,
            "object_key": object_key,
            "mime": mime,
            "width": width,
            "height": height,
            "download_status": "downloaded",
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
        }


def _extension(filename: str | None, mime: str | None) -> str:
    if filename:
        suffix = Path(filename).suffix
        if suffix and "/" not in suffix and "\\" not in suffix:
            return suffix
    guessed = mimetypes.guess_extension(mime or "")
    return guessed or ".bin"
