#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any
from urllib.parse import unquote, urlparse


DEFAULT_FIXTURE = Path("fixtures/signals/telegram_latest20/messages.latest20.json")


def main(argv: list[str]) -> int:
    fixture_path = Path(argv[1]) if len(argv) > 1 else DEFAULT_FIXTURE
    store_url = os.environ.get("SIGNAL_STORE_URL", "")
    if not store_url:
        raise SystemExit("SIGNAL_STORE_URL is required")

    db_path = sqlite_path_from_url(store_url)
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(db_path).unlink(missing_ok=True)

    messages = load_messages(fixture_path)
    first_pass = replay_messages(messages, store_url)
    first_summary = summarize_db(db_path)
    second_pass = replay_messages(messages, store_url)
    second_summary = summarize_db(db_path)

    assert first_summary["by_type"].get("new_signal", 0) == 4, first_summary
    for message_type in ("update", "noise", "position_screenshot"):
        approved = first_summary["approved_by_type"].get(message_type, 0)
        assert approved == 0, {"message_type": message_type, "approved": approved}
    assert second_summary["total"] == first_summary["total"], {
        "first_total": first_summary["total"],
        "second_total": second_summary["total"],
    }

    output = {
        "fixture": str(fixture_path),
        "store_url": store_url,
        "first_pass": first_summary,
        "second_pass": second_summary,
        "idempotent": True,
        "results": first_pass,
        "duplicate_replay_results": second_pass,
    }
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


def load_messages(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError("fixture must contain a JSON array")
    return [item for item in payload if isinstance(item, dict)]


def replay_messages(messages: list[dict[str, Any]], store_url: str) -> list[dict[str, Any]]:
    results = []
    for index, message in enumerate(messages, start=1):
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "freqtrade.signal_strategy.importer",
                "--stdin",
                "--store-url",
                store_url,
                "--approve-parsed",
                "--refresh-window",
            ],
            input=json.dumps(message, ensure_ascii=False),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        result: dict[str, Any] = {
            "index": index,
            "chatId": message.get("chatId", ""),
            "id": message.get("id", ""),
            "returncode": completed.returncode,
        }
        if completed.stdout.strip():
            try:
                result["importer"] = json.loads(completed.stdout)
            except json.JSONDecodeError:
                result["stdout"] = completed.stdout.strip()
        if completed.stderr.strip():
            result["stderr"] = completed.stderr.strip()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if completed.returncode != 0:
            raise RuntimeError(f"importer failed for message index {index}")
        results.append(result)
    return results


def summarize_db(db_path: str) -> dict[str, Any]:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT signal_id, status, message_type, pair FROM signals ORDER BY signal_id"
        ).fetchall()

    by_type: dict[str, int] = {}
    by_status: dict[str, int] = {}
    approved_by_type: dict[str, int] = {}
    for row in rows:
        message_type = str(row["message_type"])
        status = str(row["status"])
        by_type[message_type] = by_type.get(message_type, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
        if status == "approved":
            approved_by_type[message_type] = approved_by_type.get(message_type, 0) + 1

    return {
        "total": len(rows),
        "by_type": by_type,
        "by_status": by_status,
        "approved_by_type": approved_by_type,
        "signals": [dict(row) for row in rows],
    }


def sqlite_path_from_url(store_url: str) -> str:
    if store_url == "sqlite:///:memory:":
        return ":memory:"
    parsed = urlparse(store_url)
    if parsed.scheme != "sqlite":
        raise ValueError("SIGNAL_STORE_URL must use sqlite://")
    raw_path = unquote(parsed.path)
    if parsed.netloc:
        raw_path = f"{parsed.netloc}{raw_path}"
    if store_url.startswith("sqlite:////"):
        while raw_path.startswith("//"):
            raw_path = raw_path[1:]
        return f"/{raw_path.lstrip('/')}"
    if raw_path.startswith("/"):
        raw_path = raw_path[1:]
    while raw_path.startswith("//"):
        raw_path = raw_path[1:]
    return raw_path


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
