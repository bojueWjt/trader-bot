from __future__ import annotations

import asyncio

from fastapi import APIRouter, Header
from fastapi.responses import StreamingResponse

from app.dependencies import require_bearer_user
from app.services.dashboard_provider import dashboard_adapter


router = APIRouter()

HEARTBEAT_INTERVAL_SECONDS = 15.0
# every Nth heartbeat is sent as dashboard_snapshot, which makes the frontend resync
SNAPSHOT_EVERY_N_HEARTBEATS = 2


def _sse_event(event: str) -> str:
    return f"event: {event}\ndata: {{}}\n\n"


async def _event_stream():
    beat = 0
    while True:
        event = "heartbeat"
        if beat > 0 and beat % SNAPSHOT_EVERY_N_HEARTBEATS == 0:
            event = "dashboard_snapshot"
        yield _sse_event(event)
        beat += 1
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


@router.get("/stream")
async def stream() -> StreamingResponse:
    # EventSource cannot attach Authorization headers, so this endpoint carries
    # no data — it only signals liveness; resync fetches stay authenticated.
    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/overview")
def overview(authorization: str | None = Header(default=None)):
    require_bearer_user(authorization)
    return dashboard_adapter().overview()


@router.get("/open-trades")
def open_trades(authorization: str | None = Header(default=None)):
    require_bearer_user(authorization)
    return dashboard_adapter().open_trades()


@router.get("/events")
def events(authorization: str | None = Header(default=None)):
    require_bearer_user(authorization)
    return dashboard_adapter().events()
