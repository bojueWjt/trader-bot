"""T0-1b/T0-4: gateway contract checks without PostgreSQL or sockets."""

import asyncio
import hashlib
import importlib.util
import inspect
import json
import subprocess
import sys
from functools import wraps
from pathlib import Path

import httpx
import h11
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

API = Path(__file__).resolve().parents[3] / "services/control-plane/api"
sys.path.insert(0, str(API))
sys.path.insert(0, str(API.parent))
import watcher_gateway as wg  # noqa: E402

FAKE = {role: role + "-" + "x" * 40 for role in ("system_observer", "viewer", "risk_admin", "reviewer")}
FILENAME = "1234567890-1.jpg"


def expected_gateway_routes():
    source = API.parents[2] / "contracts/watcher-gateway-routes.yaml"
    command = ["python3", "-c", "import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))['routes']))", str(source)]
    rows = json.loads(subprocess.check_output(command, text=True))
    phases = sorted({row["phase"] for row in rows})
    phase_max = wg.PAYLOAD["_meta"]["phase_max"]
    assert phase_max == "P2"
    return [row for row in rows if row["identity"] == "gateway" and phases.index(row["phase"]) <= phases.index(phase_max)]


def assert_route_surface(app):
    expected = {(row["method"], row["outer_path"], "watcher_gateway__" + row["id"].replace(".", "_")) for row in expected_gateway_routes()}
    actual = {(next(iter(route.methods)), route.path, route.name) for route in app.routes if isinstance(route, APIRoute) and route.path.startswith("/v1/watcher/")}
    assert actual == expected
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/v1/watcher/"):
            assert inspect.iscoroutinefunction(route.endpoint)
            assert route.dependant.body_params == [] and route.body_field is None


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
    assert_route_surface(app)
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
    assert_route_surface(operator)
    for app in (node, ingest):
        assert not [r for r in app.routes if isinstance(r, APIRoute) and r.path.startswith("/v1/watcher/")]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=operator), base_url="http://local") as client:
        watcher = await client.get("/v1/watcher/status/", follow_redirects=False)
        assert watcher.status_code == 404 and "location" not in watcher.headers
        accounts = await client.get("/v1/accounts/", follow_redirects=False)
        assert accounts.status_code == 307 and accounts.headers["location"] == "http://local/v1/accounts" and accounts.content == b""


def test_repeated_all_role_app_middleware_is_idempotent():
    import read_api
    from app_roles import AppRole

    shared = read_api.all_role_app
    first = read_api.create_app(AppRole.ALL)
    assert first is shared
    async def serve_request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=shared), base_url="http://local") as client:
            response = await client.get("/v1/watcher/status")
            assert response.status_code == 401
    asyncio.run(serve_request())
    assert read_api.create_app(AppRole.ALL) is shared
    wg.install_middleware(shared)
    assert sum(item.cls is wg.GatewayPathMiddleware for item in shared.user_middleware) == 1
    assert shared.router.on_startup.count(read_api.watcher_config_snapshot.start_if_enabled) == 1
    assert shared.router.on_shutdown.count(read_api.watcher_config_snapshot.stop_if_started) == 1


@run_async
async def test_non_ascii_secret_keys_in_request_and_nested_response(setup):
    client, calls, responses, _, _ = setup
    response = await client.put(
        "/v1/watcher/trading/accounts/account-a",
        headers=auth("risk_admin"),
        json={"näme": "visible if accepted"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "secret_field_rejected"
    assert response.json()["details"]["fields"] == ["näme"]
    assert calls == []
    responses.append(httpx.Response(200, json={"nested": [{"näme": "hidden", "safe": 1}]}))
    response = await client.get("/v1/watcher/status", headers=auth())
    assert response.json() == {"nested": [{"safe": 1}]}
    await client.aclose()


@run_async
async def test_gateway_disabled_for_missing_or_colliding_token(setup, monkeypatch, caplog):
    client, calls, _, _, _ = setup
    monkeypatch.delenv("WATCHER_GATEWAY_TOKEN")
    missing = await client.get("/v1/watcher/status", headers=auth())
    assert missing.status_code == 503 and missing.json()["code"] == "gateway_disabled"
    monkeypatch.setenv("WATCHER_GATEWAY_TOKEN", FAKE["viewer"])
    colliding = await client.get("/v1/watcher/status", headers=auth())
    assert colliding.status_code == 503 and colliding.json()["code"] == "gateway_disabled"
    assert calls == []
    assert caplog.text.count("watcher gateway disabled") == 2
    assert FAKE["viewer"] not in caplog.text
    await client.aclose()


@run_async
async def test_auth_and_scope_precede_busy_admission(setup):
    client, calls, _, sems, _ = setup
    for _ in range(4):
        await sems["config"].acquire()
    unauthenticated = await client.get("/v1/watcher/status")
    assert unauthenticated.status_code == 401 and unauthenticated.json()["code"] == "unauthenticated"
    forbidden = await client.post("/v1/watcher/disconnect", headers=auth(), json={"api_key": "secret"})
    assert forbidden.status_code == 403 and forbidden.json()["code"] == "insufficient_scope"
    assert calls == []
    for _ in range(4):
        sems["config"].release()
    await client.aclose()


@run_async
async def test_signal_scope_and_unavailable_token_catalog(setup, monkeypatch):
    client, calls, _, _, _ = setup
    signal_token = "signal-" + "z" * 40
    monkeypatch.setenv("SIGNAL_TOKEN_ACCOUNT_A", signal_token)
    signal = await client.get("/v1/watcher/status", headers={"Authorization": "Bearer " + signal_token})
    assert signal.status_code == 403 and signal.json()["code"] == "insufficient_scope"
    monkeypatch.setenv("NAUTILUS_NODE_AUTH_JSON", "{invalid")
    unavailable_response = await client.get("/v1/watcher/status", headers=auth())
    assert unavailable_response.status_code == 503 and unavailable_response.json()["code"] == "auth_unavailable"
    assert calls == []
    await client.aclose()


@run_async
async def test_upstream_error_mapping_and_r8_shape(setup):
    client, _, responses, _, _ = setup
    path = "/v1/watcher/status"
    mapped = (
        (401, {"code": "unauthenticated"}, "upstream_status"),
        (403, {"code": "identity_forbidden"}, "upstream_status"),
        (400, {"code": "invalid_actor_headers"}, "upstream_status"),
        (404, {"code": "route_not_found"}, "upstream_status"),
        (500, {"code": "internal_error"}, "upstream_status"),
        (503, {"code": "unknown_failure"}, "upstream_status"),
        (400, {"message": "missing code"}, "malformed_response"),
        (400, {"code": "Bad Code"}, "malformed_response"),
    )
    for status, body, reason in mapped:
        responses.append(httpx.Response(status, json=body))
        response = await client.get(path, headers=auth())
        assert response.status_code == 503 and response.json()["code"] == "watcher_unavailable"
        assert response.json()["details"]["reason"] == reason
    for status, code in ((503, "db_busy"), (503, "media_too_large")):
        responses.append(httpx.Response(status, json={
            "code": code, "message": "bad\nmessage", "error": "must not leak",
            "details": {"safe": [{"näme": "secret", "api_key": "secret", "keep": 1}]},
        }))
        response = await client.get(path, headers=auth())
        body = response.json()
        assert response.status_code == status
        assert body["code"] == code and body["message"] == code
        assert body["details"] == {"safe": [{"keep": 1}]}
        assert set(body) == {"code", "message", "details", "request_id"}
    responses.append(httpx.Response(400, json={
        "code": "invalid_body", "message": "x" * 201,
        "details": ["unexpected shape"], "error": "must not leak",
    }))
    invalid_shape = await client.get(path, headers=auth())
    assert invalid_shape.status_code == 400
    assert invalid_shape.json()["message"] == "invalid_body"
    assert set(invalid_shape.json()) == {"code", "message", "request_id"}
    for failure in (httpx.ConnectTimeout("late"), httpx.ReadTimeout("late")):
        responses.append(failure)
        response = await client.get(path, headers=auth())
        assert response.status_code == 503 and response.json()["details"]["reason"] == "timeout"
    await client.aclose()


@run_async
async def test_json_cache_control_is_single_and_no_store(setup):
    client, _, responses, _, _ = setup
    responses.append(httpx.Response(200, json={"ok": True}, headers={"Cache-Control": "public, max-age=600"}))
    response = await client.get("/v1/watcher/status", headers=auth())
    assert response.headers.get_list("cache-control") == ["no-store"]
    await client.aclose()


@run_async
async def test_oversized_integer_query_is_invalid_query(setup):
    client, calls, _, _, _ = setup
    response = await client.get("/v1/watcher/trading/messages?hours=" + "9" * 4301, headers=auth())
    assert response.status_code == 400 and response.json()["code"] == "invalid_query"
    assert calls == []
    await client.aclose()


async def assert_h11_response(app, path, headers):
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "method": "GET", "scheme": "http", "root_path": "", "path": path,
        "raw_path": path.encode(), "query_string": b"", "server": ("local", 80),
        "client": ("local", 1), "http_version": "1.1",
        "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()] + [(b"host", b"local")],
    }
    messages = []
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    async def send(message):
        messages.append(message)
    await app(scope, receive, send)
    protocol = h11.Connection(h11.SERVER)
    protocol.receive_data(b"GET " + path.encode() + b" HTTP/1.1\r\nhost: local\r\n\r\n")
    while not isinstance(protocol.next_event(), h11.EndOfMessage):
        pass
    for message in messages:
        if message["type"] == "http.response.start":
            protocol.send(h11.Response(status_code=message["status"], headers=message["headers"]))
        elif message["type"] == "http.response.body":
            protocol.send(h11.Data(data=message.get("body", b"")))
            if not message.get("more_body", False):
                protocol.send(h11.EndOfMessage())
    start = next(item for item in messages if item["type"] == "http.response.start")
    body = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
    lengths = [int(value) for key, value in start["headers"] if key.lower() == b"content-length"]
    assert lengths == [len(body)]
    return start, body


@run_async
async def test_media_upstream_errors_have_valid_h11_framing(setup):
    client, _, responses, _, app = setup
    for status, code, headers in (
        (404, "not_found", {"Content-Length": "3"}),
        (416, "range_not_satisfiable", {"Content-Length": "4", "Content-Range": "bytes */100"}),
        (503, "media_too_large", {"Content-Length": "5"}),
    ):
        responses.append(httpx.Response(status, json={"code": code, "message": code}, headers=headers))
        start, body = await assert_h11_response(app, "/v1/watcher/media/" + FILENAME, auth())
        assert start["status"] == status
        assert json.loads(body)["code"] == code
        if status == 416:
            assert (b"content-range", b"bytes */100") in start["headers"]
    await client.aclose()


def test_s10_checks_secret_fields_from_yaml():
    script = (
        "from watcher_gateway_routes_lib import load_source,validate; "
        "data,_=load_source(); data['secret_fields'].append('customer_marker'); "
        "validate(data)"
    )
    result = subprocess.run(
        ["python3", "-c", script], cwd=API.parents[2] / "scripts/contracts",
        text=True, capture_output=True, check=False,
    )
    assert result.returncode != 0
    assert "S-10" in result.stderr


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
