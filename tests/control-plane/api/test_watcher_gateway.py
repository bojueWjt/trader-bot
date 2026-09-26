"""T0-1b/T0-4: gateway contract checks without PostgreSQL or sockets."""

import asyncio
import hashlib
import importlib.util
import inspect
import json
import sys
from functools import wraps
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

API = Path(__file__).resolve().parents[3] / "services/control-plane/api"
sys.path.insert(0, str(API))
sys.path.insert(0, str(API.parent))
import watcher_gateway as wg  # noqa: E402

FAKE = {role: role + "-" + "x" * 40 for role in ("system_observer", "viewer", "risk_admin", "reviewer")}
FILENAME = "1234567890-1.jpg"


def run_async(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return wrapper


@pytest.fixture
def setup(monkeypatch):
    for role, token in FAKE.items():
        monkeypatch.setenv({"system_observer": "SYSTEM_OBSERVER_TOKEN", "viewer": "VIEWER_TOKEN", "risk_admin": "RISK_ADMIN_TOKEN", "reviewer": "REVIEWER_TOKEN"}[role], token)
    monkeypatch.setenv("WATCHER_GATEWAY_TOKEN", "gateway-" + "y" * 40)
    calls = []
    responses = []

    async def handler(request):
        calls.append(request)
        if responses:
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return httpx.Response(200, json={"ok": True, "api_key": "hidden", "nested": {"token": "hidden"}}, headers={"X-Config-Revision": "4", "X-Secret": "bad"})

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://watcher")
    sems = {"config": asyncio.Semaphore(4), "media": asyncio.Semaphore(4)}
    monkeypatch.setattr(wg.gateway, "_resources", lambda budget: (sems[budget], upstream))
    app = FastAPI()
    wg.register_routes(app)
    wg.install_middleware(app)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local")
    return client, calls, responses, sems, app


def auth(role="viewer"):
    return {"Authorization": "Bearer " + FAKE[role]}


@run_async
async def test_scope_auth_order_and_generated_routes(setup):
    client, calls, _, _, app = setup
    expected = {(r["method"], r["outer_path"], "watcher_gateway__" + r["id"].replace(".", "_")) for r in wg.PAYLOAD["routes"] if r["identity"] == "gateway"}
    actual = {(next(iter(r.methods)), r.path, r.name) for r in app.routes if isinstance(r, APIRoute) and r.path.startswith("/v1/watcher/")}
    assert actual == expected and len(actual) == 24
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/v1/watcher/"):
            assert inspect.iscoroutinefunction(route.endpoint)
            assert route.dependant.body_params == [] and route.body_field is None
    for role in FAKE:
        response = await client.get("/v1/watcher/status", headers=auth(role))
        assert response.status_code == 200
    write = "/v1/watcher/disconnect"
    for role in ("viewer", "system_observer", "reviewer"):
        response = await client.post(write, headers=auth(role), json={"api_key": "x"})
        assert response.status_code == 403 and response.json()["code"] == "insufficient_scope"
    assert (await client.get("/v1/watcher/status", headers=auth())).status_code == 200
    assert (await client.get("/v1/watcher/status")).json()["code"] == "unauthenticated"
    assert (await client.get("/v1/watcher/status", headers={"Authorization": "Bearer bogus"})).json()["code"] == "invalid_token"
    assert len(calls) == 5
    for row in (r for r in wg.PAYLOAD["routes"] if r["identity"] == "gateway"):
        path = row["outer_path"]
        for name, value in {"filename": FILENAME, "account_id": "account-a", "channel_id": "1", "symbol": "BTCUSDT", "alert_id": "1"}.items():
            path = path.replace("{" + name + "}", value)
        for role in FAKE:
            response = await client.request(row["method"], path, headers=auth(role))
            if role not in row["roles"]:
                assert response.status_code == 403 and response.json()["code"] == "insufficient_scope", row["id"]
            else:
                assert response.status_code != 403, row["id"]
    await client.aclose()


@run_async
async def test_handwritten_negative_paths_and_query(setup):
    client, calls, _, _, _ = setup
    bad = ["/v1%2Fwatcher/status", "/v1/watcher%2Fstatus", "/v1/watcher/%2e/status", "/v1/watcher/%252Fstatus", "/v1/watcher/status/", "/v1/watcher//status", "/V1/Watcher/status", "/v1/watcher", "/v1/watcher/unknown"]
    for path in bad:
        response = await client.get(path, headers=auth())
        assert response.status_code == 404 and response.json()["code"] == "route_not_found", path
        assert "location" not in response.headers
    for method in ("OPTIONS", "HEAD", "POST"):
        response = await client.request(method, "/v1/watcher/status", headers=auth())
        assert response.status_code == 405 and response.headers["allow"] == "GET"
    for query in ("?bad=1", "?hours=1&hours=2"):
        response = await client.get("/v1/watcher/trading/messages" + query, headers=auth())
        assert response.status_code == 400 and response.json()["code"] == "invalid_query"
    assert calls == []
    await client.aclose()


@run_async
async def test_secret_body_headers_and_upstream_mapping(setup):
    client, calls, responses, _, _ = setup
    url = "/v1/watcher/trading/accounts/account-a"
    for key in ("api_key", "apı_key", "apİ_key"):
        response = await client.put(url, headers=auth("risk_admin"), json={key: "secret"})
        assert response.status_code == 400 and response.json()["code"] == "secret_field_rejected"
    for payload, code in (({"unknown": 1}, "invalid_body"), ([1], "invalid_body")):
        response = await client.put(url, headers=auth("risk_admin"), json=payload)
        assert response.status_code == 400 and response.json()["code"] == code
    response = await client.put(url, headers=auth("risk_admin"), content=b"x" * 65537)
    assert response.status_code == 413 and response.json()["code"] == "payload_too_large"
    assert (await client.get("/v1/watcher/status?api_key=x", headers=auth())).json()["code"] == "invalid_query"
    responses.append(httpx.Response(503, json={"code": "db_busy", "message": "busy", "details": {"a": [{"api_secret": "x", "ok": 1}]}}))
    body = {"client_ref": "abcdefgh", "expected_revision": 0, "is_enabled": False}
    response = await client.put(url, headers={**auth("risk_admin"), "Cookie": "bad", "X-Watcher-Actor": "fake", "X-Request-Id": "fake"}, json=body)
    assert response.status_code == 503 and response.json()["code"] == "db_busy"
    assert response.json()["details"] == {"a": [{"ok": 1}]}
    sent = calls[-1]
    assert sent.headers["x-watcher-actor"] == "app:risk_admin"
    assert sent.headers["x-watcher-token-fingerprint"] == hashlib.sha256(FAKE["risk_admin"].encode()).hexdigest()[:12]
    assert "cookie" not in sent.headers and "x-request-id" not in sent.headers
    assert not ({"accept-encoding", "user-agent", "connection"} & set(sent.headers))
    assert sent.headers["authorization"].startswith("Bearer gateway-")
    assert json.loads(sent.content) == body
    responses.append(httpx.Response(401, json={"code": "unauthenticated"}))
    response = await client.get("/v1/watcher/status", headers=auth())
    assert response.status_code == 503 and response.json()["code"] == "watcher_unavailable"
    responses.append(httpx.Response(302, headers={"Location": "https://bad.example/"}))
    response = await client.get("/v1/watcher/status", headers=auth())
    assert response.status_code == 503 and response.json()["details"]["reason"] == "redirect"
    for failure in (httpx.ConnectTimeout("late"), httpx.ReadTimeout("late"), httpx.WriteTimeout("late"), httpx.PoolTimeout("late"), httpx.ConnectError("down")):
        responses.append(failure)
        response = await client.get("/v1/watcher/status", headers=auth())
        assert response.status_code == 503 and response.json()["code"] == "watcher_unavailable"
    responses.append(httpx.Response(200, json={"visible": 1, "API_KEY": "secret", "nested": [{"apı_key": "secret", "safe": 2}]}))
    response = await client.get("/v1/watcher/status", headers=auth())
    assert response.json() == {"visible": 1, "nested": [{"safe": 2}]}
    responses.append(httpx.Response(200, json=[{"api_key": "secret", "name": "one"}], headers={"X-Config-Revision": "9", "Connection": "X-Config-Revision"}))
    response = await client.get("/v1/watcher/trading/accounts", headers=auth())
    assert response.json() == [{"name": "one"}] and "x-config-revision" not in response.headers
    responses.append(httpx.Response(200, json=[{"name": "two"}], headers={"X-Config-Revision": "10"}))
    response = await client.get("/v1/watcher/trading/accounts", headers=auth())
    assert response.headers["x-config-revision"] == "10"
    await client.aclose()


@run_async
async def test_busy_budgets_and_media(setup):
    client, calls, responses, sems, _ = setup
    for _ in range(4):
        await sems["config"].acquire()
    response = await client.get("/v1/watcher/status", headers=auth())
    assert response.status_code == 503 and response.json()["code"] == "gateway_busy" and not calls
    responses.append(httpx.Response(200, content=b"abc", headers={"Content-Length": "3", "Content-Type": "image/jpeg"}))
    response = await client.get("/v1/watcher/media/" + FILENAME, headers=auth())
    assert response.status_code == 200 and response.content == b"abc"
    assert sems["media"]._value == 4
    for _ in range(4):
        sems["config"].release()
    responses.append(httpx.Response(206, content=b"a", headers={"Content-Range": "bytes 0-0/3", "Content-Length": "1"}))
    response = await client.get("/v1/watcher/media/" + FILENAME, headers={**auth(), "Range": "bytes=0-0"})
    assert response.status_code == 206 and response.headers["content-range"] == "bytes 0-0/3"
    for value in ("bytes=0-", "bytes=-1"):
        responses.append(httpx.Response(206, content=b"a", headers={"Content-Range": "bytes 0-0/3", "Content-Length": "1"}))
        assert (await client.get("/v1/watcher/media/" + FILENAME, headers={**auth(), "Range": value})).status_code == 206
    response = await client.get("/v1/watcher/media/" + FILENAME, headers={**auth(), "Range": "bytes=0-1,2-3"})
    assert response.status_code == 416 and response.json()["code"] == "range_not_satisfiable"
    responses.append(httpx.Response(200, headers={"Content-Length": "3"}))
    response = await client.head("/v1/watcher/media/" + FILENAME, headers={**auth(), "Range": "bytes=0-0"})
    assert response.status_code == 200 and response.headers["content-length"] == "3" and response.content == b""
    assert "range" not in calls[-1].headers
    responses.append(httpx.Response(200, headers={"Content-Length": "20971521"}))
    response = await client.get("/v1/watcher/media/" + FILENAME, headers=auth())
    assert response.status_code == 503 and response.json()["code"] == "media_too_large"
    await client.aclose()


@run_async
async def test_truncated_media_and_cancel_cleanup(setup, caplog):
    client, _, responses, sems, _ = setup

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"a"
            raise httpx.ReadError("broken")

    responses.append(httpx.Response(200, stream=BrokenStream(), headers={"Content-Length": "3"}))
    response = await client.get("/v1/watcher/media/" + FILENAME, headers=auth())
    assert response.status_code == 200 and response.content == b"a"
    assert "truncated" in caplog.text and "bytes_sent=1" in caplog.text
    assert sems["media"]._value == 4

    class Upstream:
        closed = False

        async def aclose(self):
            self.closed = True

    upstream = Upstream()
    await sems["media"].acquire()
    async def chunks():
        yield b"a"
    response = wg.MediaStreamingResponse(chunks(), upstream=upstream, semaphore=sems["media"])
    async def cancelled_send(_):
        raise asyncio.CancelledError()
    async def receive():
        return {"type": "http.disconnect"}
    with pytest.raises(asyncio.CancelledError):
        await response({"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}}, receive, cancelled_send)
    assert upstream.closed and sems["media"]._value == 4
    await client.aclose()


@run_async
async def test_three_budgets_are_independent():
    instance = wg.Gateway()
    config, config_client = instance._resources("config")
    media, media_client = instance._resources("media")
    snapshot, snapshot_client = instance._resources("snapshot")
    assert len({id(config), id(media), id(snapshot)}) == 3
    assert len({id(config_client), id(media_client), id(snapshot_client)}) == 3
    assert config._value == 4 and media._value == 4 and snapshot._value == 1
    await asyncio.gather(config_client.aclose(), media_client.aclose(), snapshot_client.aclose())


@run_async
async def test_role_app_prefix_isolation_and_existing_redirect(setup):
    import read_api
    operator = read_api.create_app("operator-query")
    node = read_api.create_app("node-control")
    ingest = read_api.create_app("event-ingest")
    assert len([r for r in operator.routes if isinstance(r, APIRoute) and r.path.startswith("/v1/watcher/")]) == 24
    for app in (node, ingest):
        assert not [r for r in app.routes if isinstance(r, APIRoute) and r.path.startswith("/v1/watcher/")]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=operator), base_url="http://local") as client:
        watcher = await client.get("/v1/watcher/status/", follow_redirects=False)
        assert watcher.status_code == 404 and "location" not in watcher.headers
        accounts = await client.get("/v1/accounts/", follow_redirects=False)
        assert accounts.status_code == 307 and accounts.headers["location"] == "http://local/v1/accounts" and accounts.content == b""


@run_async
async def test_missing_artifact_only_disables_gateway(monkeypatch, tmp_path):
    generated = API / "generated/watcher_gateway_routes.py"
    bad = tmp_path / "watcher_gateway_routes.py"
    source = generated.read_text()
    bad.write_text(source.replace('PAYLOAD_SHA256 = "', 'PAYLOAD_SHA256 = "0', 1))
    spec = importlib.util.spec_from_file_location("tampered_watcher_routes", bad)
    module = importlib.util.module_from_spec(spec)
    with pytest.raises(ValueError, match="digest mismatch"):
        spec.loader.exec_module(module)
    monkeypatch.setattr(wg, "PAYLOAD", None)
    app = FastAPI()
    @app.get("/v1/accounts")
    async def accounts():
        return {"ok": True}
    wg.register_routes(app)
    wg.install_middleware(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
        result = await client.get("/v1/watcher/status")
        assert result.status_code == 503 and result.json()["code"] == "gateway_disabled"
        assert (await client.get("/v1/accounts")).json() == {"ok": True}
        assert (await client.get("/v1/accounts/", follow_redirects=False)).status_code == 307
