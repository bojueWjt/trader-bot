from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_json_atomic(
    path: str | Path,
    payload: Any,
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
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
        os.replace(tmp_name, target)
        _fsync_directory(target.parent)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    flags |= int(getattr(os, "O_DIRECTORY", 0))
    fd = os.open(str(path), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
