import os

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx

from app.dashboard import freqtrade_router
from app.dashboard.router import router as dashboard_router
from app.dashboard.freqtrade_router import router as freqtrade_proxy_router
from app.dashboard.system_router import router as system_router


def build_client():
    app = FastAPI()
    app.include_router(dashboard_router, prefix="/api/dashboard")
    app.include_router(system_router, prefix="/api/system")
    app.include_router(freqtrade_proxy_router, prefix="/api/freqtrade")
    return TestClient(app)


def auth_headers(role="system_observer"):
    env_by_role = {
        "risk_admin": "RISK_ADMIN_TOKEN",
        "viewer": "VIEWER_TOKEN",
        "reviewer": "REVIEWER_TOKEN",
        "system_observer": "SYSTEM_OBSERVER_TOKEN",
        "nautilus_node": "NAUTILUS_NODE_TOKEN",
    }
    token = os.environ[env_by_role[role]]
    return {"authorization": f"Bearer {token}"}


def test_dashboard_overview_returns_complete_fake_snapshot():
    client = build_client()

    response = client.get("/api/dashboard/overview", headers=auth_headers())

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "bot_status",
        "run_mode",
        "equity",
        "free_balance",
        "margin_used",
        "realized_pnl_today",
        "unrealized_pnl",
        "open_trade_count",
        "open_order_count",
        "today_signal_count",
        "today_executed_count",
        "risk_state",
        "recent_events",
    }
    assert body["open_trade_count"] == 2
    assert body["recent_events"][0]["event_id"] == "evt-risk-001"


def test_dashboard_open_trades_include_signal_ids():
    client = build_client()

    response = client.get("/api/dashboard/open-trades", headers=auth_headers())

    assert response.status_code == 200
    body = response.json()
    trade = body["open_trades"][0]
    assert set(trade) == {
        "trade_id",
        "signal_id",
        "pair",
        "side",
        "entry_price",
        "current_price",
        "stake_amount",
        "leverage",
        "unrealized_pnl",
        "opened_at",
        "status",
    }
    assert trade["signal_id"] == "sig-btc-breakout-001"


def test_dashboard_events_include_event_ids():
    client = build_client()

    response = client.get("/api/dashboard/events", headers=auth_headers())

    assert response.status_code == 200
    body = response.json()
    event = body["events"][0]
    assert set(event) == {
        "event_id",
        "event_type",
        "occurred_at",
        "severity",
        "message",
        "correlation_id",
        "source",
    }
    assert event["event_id"] == "evt-risk-001"


def test_dashboard_read_endpoints_reject_unauthenticated():
    # M5-01 regression guard: data endpoints must not leak without a login token.
    client = build_client()

    for path in ("/api/dashboard/overview", "/api/dashboard/open-trades",
                 "/api/dashboard/events", "/api/freqtrade/status"):
        assert client.get(path).status_code == 401, path


def test_dashboard_stream_stays_public_for_eventsource():
    # SSE heartbeat must remain reachable without Authorization (EventSource limitation).
    from app.dashboard.router import stream
    import asyncio

    async def first_event():
        response = await stream()
        iterator = response.body_iterator
        try:
            return await iterator.__anext__()
        finally:
            await iterator.aclose()

    assert "event: heartbeat" in asyncio.run(first_event())


def test_system_snapshot_returns_required_fields_for_observer():
    client = build_client()

    response = client.get("/api/system/snapshot", headers=auth_headers())

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"account", "positions", "signals", "risk", "events"}
    assert set(body["account"]) == {"equity", "balance", "margin_used", "margin_free"}
    assert {"symbol", "side", "size", "entry_price", "unrealized_pnl"}.issubset(body["positions"][0])
    assert {"id", "symbol", "direction", "status", "created_at"}.issubset(body["signals"][0])
    assert set(body["risk"]) == {"daily_loss", "drawdown", "limits", "current_usage"}
    assert set(body["events"][0]) == {"type", "message", "ts"}


def test_freqtrade_status_proxies_to_upstream_mock(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/status"
        return httpx.Response(200, json={"status": "running"})

    monkeypatch.setattr(
        freqtrade_router,
        "build_freqtrade_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client = build_client()

    response = client.get("/api/freqtrade/status", headers=auth_headers())

    assert response.status_code == 200
    assert response.json() == {"status": "running"}


def test_freqtrade_non_whitelisted_path_returns_404():
    client = build_client()

    response = client.get("/api/freqtrade/config", headers=auth_headers())

    assert response.status_code == 404


def test_freqtrade_write_method_returns_405():
    client = build_client()

    response = client.post("/api/freqtrade/status")

    assert response.status_code == 405


def test_freqtrade_upstream_unreachable_returns_503(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(
        freqtrade_router,
        "build_freqtrade_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client = build_client()

    response = client.get("/api/freqtrade/status", headers=auth_headers())

    assert response.status_code == 503
    assert response.json()["error"] == "connection_error"


def test_dashboard_stream_emits_immediate_heartbeat_sse():
    # TestClient hangs closing infinite SSE streams, so exercise the endpoint
    # object directly: response shape + first event before any sleep.
    import asyncio

    from app.dashboard.router import stream

    async def first_event():
        response = await stream()
        assert response.media_type == "text/event-stream"
        assert response.headers["cache-control"] == "no-cache"
        iterator = response.body_iterator
        try:
            return await iterator.__anext__()
        finally:
            await iterator.aclose()

    first_chunk = asyncio.run(first_event())
    assert "event: heartbeat" in first_chunk
