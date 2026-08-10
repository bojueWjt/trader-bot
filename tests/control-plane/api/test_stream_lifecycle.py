from __future__ import annotations

import asyncio
import json
from time import monotonic

import read_api


class _RequestState:
    def __init__(self, *, disconnected: bool) -> None:
        self.calls = 0
        self.disconnected = disconnected

    async def is_disconnected(self) -> bool:
        self.calls += 1
        return self.disconnected


async def _read_stream(request: _RequestState) -> str:
    response = await read_api.v1_stream(
        request=request,
        authorization="Bearer observer-token",
    )
    chunks = []
    async for chunk in response.body_iterator:
        if isinstance(chunk, bytes):
            chunks.append(chunk.decode("utf-8"))
            continue
        chunks.append(chunk)
    return "".join(chunks)


def test_stream_is_lightweight_and_ends_at_connection_lifetime(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SYSTEM_OBSERVER_TOKEN", "observer-token")
    monkeypatch.setattr(read_api, "_STREAM_INTERVAL_S", 0.01)
    monkeypatch.setattr(read_api, "_STREAM_MAX_AGE_S", 0.035)

    def fail_if_database_is_opened(*_args, **_kwargs):
        raise AssertionError("SSE notifications must not query PostgreSQL")

    monkeypatch.setattr(read_api.psycopg2, "connect", fail_if_database_is_opened)
    request = _RequestState(disconnected=False)

    started_at = monotonic()
    body = asyncio.run(_read_stream(request))
    elapsed = monotonic() - started_at

    assert elapsed < 0.5
    assert request.calls >= 2
    assert body.count("event: dashboard_snapshot\n") >= 1
    assert body.count("event: heartbeat\n") >= 1
    assert "event: error\n" not in body
    assert "data: {}\n\n" in body
    assert len(body.encode("utf-8")) < 2048

    heartbeat_line = next(
        line
        for line in body.splitlines()
        if line.startswith('data: {"ts":')
    )
    heartbeat = json.loads(heartbeat_line.removeprefix("data: "))
    assert heartbeat["ts"].endswith("+00:00")


def test_stream_stops_before_emitting_after_client_disconnect(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SYSTEM_OBSERVER_TOKEN", "observer-token")
    monkeypatch.setattr(read_api, "_STREAM_INTERVAL_S", 0.01)
    monkeypatch.setattr(read_api, "_STREAM_MAX_AGE_S", 1.0)
    request = _RequestState(disconnected=True)

    body = asyncio.run(_read_stream(request))

    assert body == ""
    assert request.calls == 1
