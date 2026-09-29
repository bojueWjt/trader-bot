"""WGW-1.0.4 watcher-gateway role (backend-api.md §9.14.6, §9.16 B-1/B-5, B-8..B-12).

No PostgreSQL and no YAML import. The oracle for the other roles' route
tables is ``data/role_routes_before_wac104.json``, generated from the commit
before this change (f5181d9), not from the code under test.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import sysconfig
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest
from fastapi.routing import APIRoute

REPO = Path(__file__).resolve().parents[3]
API = REPO / "services/control-plane/api"
for _p in (str(API), str(API.parent), str(API.parent / "db")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import app_roles  # noqa: E402
import pools  # noqa: E402
import read_api  # noqa: E402
import watcher_gateway as wg  # noqa: E402
from app_roles import AppRole  # noqa: E402

ORACLE = json.loads((Path(__file__).parent / "data/role_routes_before_wac104.json").read_text())
READER_ENV = {
    "system_observer": "SYSTEM_OBSERVER_TOKEN",
    "viewer": "VIEWER_TOKEN",
    "risk_admin": "RISK_ADMIN_TOKEN",
    "reviewer": "REVIEWER_TOKEN",
}
PHASES = ("P0", "P1", "P2", "P3")
# Written independently of the generator and of the check script (§9.16 B-5).
DIRECT_GUARD = re.compile(r"^(?i:/v1/watcher)(?:[/\n%]|$)")
VALUE_POOL = ("x", "account-a", "A.b@c-1", "BTCUSDT", "-1001234567890", "1700000000000-1.png", "1", "1-1.jpg")
RESIDUAL_SHAPES = (
    "/v1/watcher/../accounts",
    "/v1/watcher/..%2Faccounts",
    "/v1/watcher%2e%2e/accounts",
    "/v1/watcher%25",
    "/v1/watcher//status",
    "/v1/watcher/./status",
    "/v1/watcher/status/..",
)
NOT_FOUND = b'{"detail":"Not Found"}'


def _token(prefix: str) -> str:
    return prefix + "-" + secrets.token_hex(24)


@pytest.fixture
def tokens(monkeypatch):
    for name in (
        "CONTROL_PLANE_APP_ROLE", "AUTH_SECRET_KEY", "DATABASE_URL",
        "CONTROL_PLANE_EXPECT_DATABASE_ROLE", "NAUTILUS_NODE_AUTH_JSON",
        "SIGNAL_TOKEN_ACCOUNT_A", "SIGNAL_TOKEN_ACCOUNT_B",
        "SIGNAL_TOKEN_ACCOUNT_C", "SIGNAL_TOKEN_ACCOUNT_D",
        "NAUTILUS_NODE_TOKEN", "WATCHER_GATEWAY_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    values = {role: _token(role) for role in READER_ENV}
    for role, env in READER_ENV.items():
        monkeypatch.setenv(env, values[role])
    values["gateway"] = _token("gateway")
    monkeypatch.setenv("WATCHER_GATEWAY_TOKEN", values["gateway"])
    return values


@pytest.fixture
def upstream(monkeypatch):
    calls = []

    async def handler(request):
        calls.append(request)
        if request.url.path.startswith("/media/"):
            headers = {"Content-Length": "3", "Content-Type": "image/png"}
            return httpx.Response(200, content=b"" if request.method == "HEAD" else b"abc", headers=headers)
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://watcher")
    semaphores = {"config": asyncio.Semaphore(4), "media": asyncio.Semaphore(4)}
    monkeypatch.setattr(wg.gateway, "_resources", lambda budget: (semaphores[budget], client))
    return calls


def _gateway_rows(source=None):
    source = wg.PAYLOAD if source is None else source
    phase_max = PHASES.index(source["_meta"]["phase_max"])
    return [r for r in source["routes"] if r["identity"] == "gateway" and PHASES.index(r["phase"]) <= phase_max]


def _gateway_names():
    return {"watcher_gateway__" + row["id"].replace(".", "_") for row in _gateway_rows()}


def _concrete(template):
    path = template
    for name, value in {"filename": "1700000000000-1.png", "account_id": "account-a", "channel_id": "1", "symbol": "BTCUSDT", "alert_id": "1"}.items():
        path = path.replace("{" + name + "}", value)
    return path


async def _asgi(app, method, path, raw_path=None, headers=()):
    """Drive one request at the ASGI scope level (explicit path / raw_path)."""
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "method": method, "scheme": "http", "root_path": "", "path": path,
        "raw_path": (raw_path if raw_path is not None else path).encode("latin1"),
        "query_string": b"", "server": ("local", 80), "client": ("local", 1),
        "http_version": "1.1",
        "headers": [(b"host", b"local")] + [(k.lower().encode(), v.encode()) for k, v in headers],
    }
    await app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return start["status"], body


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------- B-8 roles

def test_b8_role_enum_spellings_and_no_database_role():
    assert AppRole.WATCHER_GATEWAY.value == "watcher-gateway"
    assert app_roles.resolve_app_role("watcher-gateway") is AppRole.WATCHER_GATEWAY
    assert app_roles.resolve_app_role("watcher_gateway") is AppRole.WATCHER_GATEWAY
    assert app_roles.resolve_app_role(" Watcher_Gateway ") is AppRole.WATCHER_GATEWAY
    assert AppRole.WATCHER_GATEWAY not in app_roles._DATABASE_ROLE_NAMES
    assert AppRole.WATCHER_GATEWAY not in app_roles._ROLLBACK_ONLY_PERMISSION_PROBES
    assert app_roles.database_role_name(AppRole.WATCHER_GATEWAY) is False
    assert app_roles.rollback_only_permission_probe(AppRole.WATCHER_GATEWAY) is False
    with pytest.raises(RuntimeError):
        app_roles.expected_database_role_name(AppRole.WATCHER_GATEWAY)


def test_b8_route_names_come_from_payload_and_operator_query_excludes_gateway():
    expected = _gateway_names()
    assert expected and all(name.startswith("watcher_gateway__") for name in expected)
    assert wg.gateway_route_names() == frozenset(expected)
    # The shared app has no gateway routes; the names still come out of the payload.
    assert not [r for r in read_api.all_role_app.routes if getattr(r, "name", "").startswith("watcher_gateway__")]
    assert app_roles.route_names_for_role(read_api.all_role_app.routes, AppRole.WATCHER_GATEWAY) == frozenset(expected | {"role_database_health"})

    class FakeRoute(APIRoute):
        def __init__(self, name):
            self.name = name

    injected = list(read_api.all_role_app.routes) + [FakeRoute("watcher_gateway__injected")]
    names = app_roles.route_names_for_role(injected, AppRole.OPERATOR_QUERY)
    assert not {n for n in names if n.startswith("watcher_gateway__")}
    assert names == app_roles.route_names_for_role(read_api.all_role_app.routes, AppRole.OPERATOR_QUERY)


# ----------------------------------------------------- B-10 (a) other roles

@pytest.mark.parametrize("role", ["operator-query", "node-control", "event-ingest", "all"])
def test_b10a_other_roles_have_no_gateway(role, tokens, upstream):
    app = read_api.create_app(role)
    assert not [r for r in app.routes if getattr(r, "name", "").startswith("watcher_gateway__")]
    assert not [r for r in app.routes if getattr(r, "path", "").lower().startswith("/v1/watcher")]
    assert not [m for m in app.user_middleware if m.cls is wg.GatewayPathMiddleware]
    stack = app.build_middleware_stack()
    seen = []
    while stack is not None and len(seen) < 50:
        seen.append(type(stack).__name__)
        stack = getattr(stack, "app", None)
    assert "GatewayPathMiddleware" not in seen

    async def probe():
        results = []
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            for path in ("/v1/watcher/status", "/v1/watcher/status/", "/V1/WATCHER/status"):
                response = await client.get(path, headers={"Authorization": "Bearer " + tokens["system_observer"]}, follow_redirects=False)
                results.append((path, response.status_code, response.content, response.headers.get("location")))
            accounts = await client.get("/v1/accounts/", follow_redirects=False)
        return results, accounts

    results, accounts = _run(probe())
    if role in ("operator-query", "all"):
        for path, status, content, location in results:
            assert (status, content, location) == (404, NOT_FOUND, None), path
    else:
        for path, status, _content, _location in results:
            assert status == 404, path
    assert upstream == []
    before = ORACLE["accounts_trailing_slash"][role]
    assert accounts.status_code == before["status"]
    assert [[k.decode("latin1"), v.decode("latin1")] for k, v in accounts.headers.raw] == before["headers"]
    assert accounts.content.hex() == before["body_hex"]


# ---------------------------------------------------- B-10 (c) route oracle

@pytest.mark.parametrize("role", ["node-control", "event-ingest", "operator-query", "all"])
def test_b10c_other_roles_route_tables_match_pre_change_oracle(role):
    app = read_api.create_app(role)
    actual = sorted(
        [route.name, route.path, sorted(route.methods)]
        for route in app.routes
        if isinstance(route, APIRoute)
    )
    assert actual == ORACLE[role]
    assert len(actual) > 0


# ------------------------------------------------- B-10 (b) gateway role app

def test_b10b_watcher_gateway_app_surface(tokens, upstream, monkeypatch):
    monkeypatch.delenv("CONTROL_PLANE_APP_ROLE", raising=False)
    app = read_api.create_app("watcher-gateway")
    assert app is not read_api.all_role_app
    assert app.docs_url is None and app.redoc_url is None and app.openapi_url is None
    api_routes = [r for r in app.routes if isinstance(r, APIRoute)]
    assert len(api_routes) == len(app.routes)
    assert {r.name for r in api_routes} == _gateway_names() | {"role_database_health"}
    expected = {(row["method"], row["outer_path"], "watcher_gateway__" + row["id"].replace(".", "_")) for row in _gateway_rows()}
    actual = {(next(iter(r.methods)), r.path, r.name) for r in api_routes if r.path.startswith("/v1/watcher/")}
    assert actual == expected and len(actual) > 0
    for route in api_routes:
        if route.path.startswith("/v1/watcher/"):
            assert asyncio.iscoroutinefunction(route.endpoint)
            assert route.dependant.body_params == [] and route.body_field is None
    assert [m.cls for m in app.user_middleware].count(wg.GatewayPathMiddleware) == 1
    assert [m.cls for m in app.user_middleware].count(app_roles.BindRequestRoleMiddleware) == 1
    assert read_api.watcher_config_snapshot.start_if_enabled not in app.router.on_startup
    assert read_api.watcher_config_snapshot.stop_if_started not in app.router.on_shutdown
    assert app.router.on_startup == [] and app.router.on_shutdown == []
    assert read_api._PSYCOPG2_DRIVER.errors.QueryCanceled not in app.exception_handlers

    async def probe():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            out = {}
            for path in ("/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"):
                out[path] = (await client.get(path)).status_code
            for method, path in (("GET", "/v1/accounts"), ("GET", "/v1/operator/orders"), ("POST", "/v1/operator/orders"), ("GET", "/api/system/snapshot")):
                out[method + " " + path] = (await client.request(method, path, headers={"Authorization": "Bearer " + tokens["risk_admin"]})).status_code
            slash = await client.get("/v1/watcher/status/", headers={"Authorization": "Bearer " + tokens["viewer"]}, follow_redirects=False)
            health = await client.get("/health/role")
            return out, slash, health

    out, slash, health = _run(probe())
    assert all(status == 404 for status in out.values()), out
    assert slash.status_code == 404 and slash.json()["code"] == "route_not_found" and "location" not in slash.headers
    # CONTROL_PLANE_APP_ROLE is unset: only bind_request_role makes this the gateway branch.
    assert os.environ.get("CONTROL_PLANE_APP_ROLE") is None
    assert app_roles.current_app_role() is AppRole.ALL
    assert health.status_code == 200
    assert health.json() == {"status": "healthy", "app_role": "watcher-gateway", "database": "none", "gateway": "enabled"}
    assert upstream == []


# ------------------------------------------------------------- B-5 on role

def test_b5_1_g1_implies_direct_guard(tokens, monkeypatch):
    app = read_api.create_app("watcher-gateway")
    seen = []

    async def fake_handle(request, row):
        seen.append((row["id"], request.scope["state"].get("watcher_gateway_route_id")))
        from fastapi.responses import Response
        return Response(status_code=204)

    monkeypatch.setattr(wg.gateway, "handle", fake_handle)
    params = wg.PAYLOAD["path_params"]
    candidates = []
    for row in _gateway_rows():
        paths = [row["outer_path"]]
        for name in re.findall(r"\{([^{}]+)\}", row["outer_path"]):
            values = [v for v in VALUE_POOL if re.fullmatch(params[name]["gateway_pattern"], v)]
            assert values, name
            paths = [p.replace("{" + name + "}", v, 1) for p in paths for v in values]
        candidates.extend((row, path) for path in paths)
    assert len(candidates) > len(_gateway_rows())

    async def run_all():
        for row, path in candidates:
            status, _ = await _asgi(app, row["method"], path)
            assert status == 204, (row["id"], path)
    _run(run_all())
    assert len(seen) == len(candidates)
    assert all(route_id == state_id for route_id, state_id in seen)
    for _row, path in candidates:
        assert DIRECT_GUARD.search(path) is not None, path


def test_b5_2_residual_shapes_rejected_before_auth_with_positive_control(tokens, upstream, monkeypatch):
    app = read_api.create_app("watcher-gateway")
    count = {"n": 0}
    original = wg.resolve_principal

    def spy(*args, **kwargs):
        count["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(wg, "resolve_principal", spy)
    auth = [("Authorization", "Bearer " + tokens["system_observer"])]

    async def run_all():
        for raw in RESIDUAL_SHAPES:
            for method in ("GET", "HEAD"):
                status, body = await _asgi(app, method, unquote(raw), raw, auth)
                assert status == 404, (method, raw)
                if method == "GET":
                    assert json.loads(body)["code"] == "route_not_found", raw
        assert count["n"] == 0
        assert upstream == []
        status, body = await _asgi(app, "GET", "/v1/watcher/status", headers=auth)
        return status, body

    status, body = _run(run_all())
    assert count["n"] == 1
    assert status == 200 and json.loads(body) == {"ok": True}
    assert len(upstream) == 1 and upstream[0].url.path == "/api/status"


def test_b5_3_and_b1_error_codes_on_watcher_gateway(tokens, upstream):
    app = read_api.create_app("watcher-gateway")
    signal_shaped = _token("signal")
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
    claims = base64.urlsafe_b64encode(json.dumps({"sub": "u", "role": "risk_admin", "exp": 4102444800}).encode()).rstrip(b"=").decode()
    jwt_shaped = header + "." + claims + "." + base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    assert "AUTH_SECRET_KEY" not in os.environ and not any(k.startswith("SIGNAL_TOKEN_") for k in os.environ)

    async def run_all():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            ok = await client.get("/v1/watcher/status", headers={"Authorization": "Bearer " + tokens["system_observer"]})
            signal = await client.get("/v1/watcher/status", headers={"Authorization": "Bearer " + signal_shaped})
            session = await client.get("/v1/watcher/status", headers={"Authorization": "Bearer " + jwt_shaped})
            random_token = await client.get("/v1/watcher/status", headers={"Authorization": "Bearer " + _token("random")})
            missing = await client.get("/v1/watcher/status")
            return ok, signal, session, random_token, missing

    ok, signal, session, random_token, missing = _run(run_all())
    assert ok.status_code == 200 and ok.json() == {"ok": True}
    assert (signal.status_code, signal.json()["code"]) == (403, "invalid_token")
    assert (session.status_code, session.json()["code"]) == (401, "unauthenticated")
    assert (random_token.status_code, random_token.json()["code"]) == (403, "invalid_token")
    assert (missing.status_code, missing.json()["code"]) == (401, "unauthenticated")
    assert len(upstream) == 1


# ---------------------------------------------------------------- B-12 health

HEALTH_OK = {"status": "healthy", "app_role": "watcher-gateway", "database": "none", "gateway": "enabled"}
HEALTH_DOWN = {"status": "unhealthy", "app_role": "watcher-gateway", "database": "none", "gateway": "disabled"}


def _health(app):
    async def get():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            return await client.get("/health/role")
    return _run(get())


def test_b12_health_enabled_missing_and_colliding_token(tokens, monkeypatch, caplog):
    connects = []
    monkeypatch.setattr(read_api._PSYCOPG2_DRIVER, "connect", lambda *a, **k: connects.append(a) or (_ for _ in ()).throw(AssertionError("db")))
    app = read_api.create_app("watcher-gateway")
    ok = _health(app)
    assert (ok.status_code, ok.json()) == (200, HEALTH_OK)
    monkeypatch.delenv("WATCHER_GATEWAY_TOKEN")
    missing = _health(app)
    assert (missing.status_code, missing.json()) == (503, HEALTH_DOWN)
    monkeypatch.setenv("WATCHER_GATEWAY_TOKEN", "")
    empty = _health(app)
    assert (empty.status_code, empty.json()) == (503, HEALTH_DOWN)
    monkeypatch.setenv("WATCHER_GATEWAY_TOKEN", tokens["reviewer"])
    colliding = _health(app)
    assert (colliding.status_code, colliding.json()) == (503, HEALTH_DOWN)
    monkeypatch.setenv("WATCHER_GATEWAY_TOKEN", tokens["gateway"])
    monkeypatch.setenv("NAUTILUS_NODE_AUTH_JSON", "{invalid")
    catalog = _health(app)
    assert (catalog.status_code, catalog.json()) == (503, HEALTH_DOWN)
    monkeypatch.delenv("NAUTILUS_NODE_AUTH_JSON")
    assert _health(app).status_code == 200
    assert connects == []
    for value in tokens.values():
        assert value not in caplog.text


def test_b12_health_disabled_for_four_artifact_injections(tokens, upstream, caplog):
    original = wg.PAYLOAD
    collision = copy.deepcopy(original)
    collision["routes"].append({**copy.deepcopy(_gateway_rows(original)[0]), "id": "gw.injected.get", "outer_path": "/v1/watcher/login/x", "inner_path": "/api/login/x"})
    empty = copy.deepcopy(original)
    empty["never_allowed"] = []
    methods = copy.deepcopy(original)
    methods["never_allowed"][0]["methods"] = ["GET"]
    injections = [
        lambda: wg.load_route_artifact(collision),
        lambda: wg.load_route_artifact(empty),
        lambda: wg.load_route_artifact(methods),
        lambda: wg.load_route_artifact(loader=lambda _name: (_ for _ in ()).throw(ImportError("fixture"))),
    ]
    try:
        for inject in injections:
            assert inject() is False
            app = read_api.create_app("watcher-gateway")
            down = _health(app)
            assert (down.status_code, down.json()) == (503, HEALTH_DOWN)

            async def probe():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
                    return await client.get("/v1/watcher/status", headers={"Authorization": "Bearer " + tokens["viewer"]})
            response = _run(probe())
            assert response.status_code == 503 and response.json()["code"] == "gateway_disabled"
            assert set(response.json()) == {"code", "message", "request_id"}
            assert wg.load_route_artifact(original) is True
            assert _health(read_api.create_app("watcher-gateway")).json() == HEALTH_OK
        assert upstream == []
        for value in tokens.values():
            assert value not in caplog.text
    finally:
        assert wg.load_route_artifact(original)


@pytest.mark.parametrize(
    ("role", "status", "detail"),
    [
        ("all", 503, "isolated control-plane app role is required"),
        ("operator-query", 503, "database role health is unavailable"),
        ("node-control", 503, "database role health is unavailable"),
        ("event-ingest", 503, "database role health is unavailable"),
    ],
)
def test_b12_other_roles_health_unchanged_without_database(role, status, detail, tokens):
    response = _health(read_api.create_app(role))
    assert (response.status_code, response.json()) == (status, {"detail": detail})


# ------------------------------------------------ B-11 database fail-closed

def test_b11_database_access_fails_closed_for_watcher_gateway(monkeypatch):
    connects = []
    monkeypatch.setattr(read_api._PSYCOPG2_DRIVER, "connect", lambda *a, **k: connects.append(a))
    with pytest.raises(pools.PoolConfigurationError):
        pools.checkout_role_connection("postgresql://127.0.0.1:1/none", AppRole.WATCHER_GATEWAY)
    with pytest.raises(pools.PoolConfigurationError):
        pools.pool_config_for_role("watcher-gateway")
    token = app_roles.bind_app_role(AppRole.WATCHER_GATEWAY)
    try:
        with pytest.raises(pools.PoolConfigurationError):
            read_api._database_connection("postgresql://127.0.0.1:1/none")
    finally:
        app_roles.reset_app_role(token)
    assert pools.close_role_pools(AppRole.WATCHER_GATEWAY) is None
    assert pools.close_role_pools("watcher_gateway") is None
    with pytest.raises(pools.PoolConfigurationError):
        pools.close_role_pools("not-a-role")
    assert connects == []


CHILD = r'''
import sys, os
EVENTS = []
OPENERS = {}
def _opener():
    frame = sys._getframe(2)
    while frame is not None:
        name = frame.f_code.co_filename
        if not name.startswith("<"):
            return name
        frame = frame.f_back
    return None
def _hook(event, args):
    if event == "open":
        path = args[0]
        if isinstance(path, (str, bytes)):
            path = os.fsdecode(path)
            EVENTS.append(("open", path))
            OPENERS.setdefault(os.path.realpath(path), _opener())
    elif event == "import":
        if len(args) > 1 and isinstance(args[1], str):
            EVENTS.append(("import", args[1]))
sys.addaudithook(_hook)

import asyncio, json, sysconfig
CODE_ROOT = sys.argv[1]
os.chdir(os.path.join(CODE_ROOT, "services/control-plane/api"))
sys.path.insert(0, os.getcwd())

import psycopg2
COUNTS = {"psycopg2.connect": 0, "checkout_role_connection": 0, "_database_connection": 0, "BoundedPostgresPool": 0}
_real_connect = psycopg2.connect
def _connect_spy(*a, **k):
    COUNTS["psycopg2.connect"] += 1
    raise RuntimeError("database access is forbidden in this test")
psycopg2.connect = _connect_spy

import read_api, pools, app_roles
_real_checkout = pools.checkout_role_connection
def _checkout_spy(*a, **k):
    COUNTS["checkout_role_connection"] += 1
    return _real_checkout(*a, **k)
pools.checkout_role_connection = _checkout_spy
read_api.checkout_role_connection = _checkout_spy
_real_dbc = read_api._database_connection
def _dbc_spy(*a, **k):
    COUNTS["_database_connection"] += 1
    return _real_dbc(*a, **k)
read_api._database_connection = _dbc_spy
_real_pool_init = pools.BoundedPostgresPool.__init__
def _pool_spy(self, *a, **k):
    COUNTS["BoundedPostgresPool"] += 1
    return _real_pool_init(self, *a, **k)
pools.BoundedPostgresPool.__init__ = _pool_spy

import httpx
app = read_api.app
MEDIA = "1700000000000-1.png"
UPSTREAM = []

async def stub(reader, writer):
    head = await reader.readuntil(b"\r\n\r\n")
    method, target, _ = head.split(b"\r\n", 1)[0].decode().split(" ", 2)
    UPSTREAM.append((method, target))
    if target == "/api/status":
        body = b'{"configured":true}'
        headers = b"Content-Type: application/json\r\n"
    elif target == "/media/" + MEDIA:
        body = b"\x89PNG"
        headers = b"Content-Type: image/png\r\n"
    else:
        body = b'{"code":"not_found","message":"x"}'
        headers = b"Content-Type: application/json\r\n"
    status = b"200 OK" if target in ("/api/status", "/media/" + MEDIA) else b"404 Not Found"
    writer.write(b"HTTP/1.1 " + status + b"\r\n" + headers + b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + (b"" if method == "HEAD" else body))
    await writer.drain()
    writer.close()

async def lifespan(kind, queue_in, queue_out):
    await queue_in.put({"type": "lifespan." + kind})
    return await queue_out.get()

async def main():
    port = int(os.environ["WATCHER_GATEWAY_URL"].rsplit(":", 1)[1])
    server = await asyncio.start_server(stub, "127.0.0.1", port)
    q_in, q_out = asyncio.Queue(), asyncio.Queue()
    lifespan_task = asyncio.create_task(app({"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}, q_in.get, q_out.put))
    started = await lifespan("startup", q_in, q_out)
    results = {"startup": started["type"], "role": getattr(app.state, "control_plane_role", None)}
    observer = {"Authorization": "Bearer " + os.environ["SYSTEM_OBSERVER_TOKEN"]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        r = await client.get("/v1/watcher/status", headers=observer)
        results["ok"] = [r.status_code, r.json()]
        r = await client.get("/v1/watcher/status")
        results["missing"] = [r.status_code, r.json()["code"]]
        r = await client.get("/v1/watcher/status", headers={"Authorization": "Bearer " + sys.argv[2]})
        results["random"] = [r.status_code, r.json()["code"]]
        r = await client.get("/v1/watcher/status", headers={"Authorization": "Bearer " + sys.argv[3]})
        results["signal"] = [r.status_code, r.json()["code"]]
        r = await client.get("/v1/watcher/media/" + MEDIA, headers=observer)
        results["media_get"] = [r.status_code, r.content.hex(), r.headers.get("content-type")]
        r = await client.head("/v1/watcher/media/" + MEDIA, headers=observer)
        results["media_head"] = [r.status_code, r.headers.get("content-length"), len(r.content)]
        r = await client.get("/health/role")
        results["health"] = [r.status_code, r.json()]
        r = await client.get("/v1/accounts")
        results["accounts"] = r.status_code
    stopped = await lifespan("shutdown", q_in, q_out)
    results["shutdown"] = stopped["type"]
    await lifespan_task
    server.close()
    await server.wait_closed()
    return results

results = asyncio.run(main())
results["upstream"] = UPSTREAM
results["counts_after_requests"] = dict(COUNTS)
active = {}
try:
    _real_checkout("postgresql://127.0.0.1:1/none", app_roles.current_app_role())
    active["checkout_role_connection"] = "no error"
except Exception as exc:
    active["checkout_role_connection"] = type(exc).__name__
try:
    _real_dbc("postgresql://127.0.0.1:1/none")
    active["_database_connection"] = "no error"
except Exception as exc:
    active["_database_connection"] = type(exc).__name__
try:
    pools.BoundedPostgresPool("postgresql://127.0.0.1:1/none", pools.pool_config_for_role(app_roles.current_app_role()))
    active["BoundedPostgresPool"] = "no error"
except Exception as exc:
    active["BoundedPostgresPool"] = type(exc).__name__
results["active"] = active
results["connects_total"] = COUNTS["psycopg2.connect"]
results["current_role"] = app_roles.current_app_role().value
files = {p for _, p in EVENTS}
files.update(getattr(m, "__file__", None) for m in list(sys.modules.values()))
files.discard(None)
results["files"] = sorted(os.path.realpath(p) for p in files if p and not p.startswith("<"))
results["openers"] = {path: (os.path.realpath(opener) if opener else None) for path, opener in OPENERS.items()}
results["roots"] = {
    "code_root": os.path.realpath(CODE_ROOT),
    "prefix": os.path.realpath(sys.prefix),
    "stdlib": sorted({os.path.realpath(sysconfig.get_paths()[k]) for k in ("stdlib", "platstdlib")} | {os.path.realpath(sys.base_prefix)}),
}
print("RESULT " + json.dumps(results))
'''

CODE_SUBSET = ("services/control-plane", "services/nautilus-node/observability", "packages", "db/migrations")


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_b11_isolated_code_root_whitelist_env_no_database_no_outside_files(tmp_path):
    code_root = tmp_path / "releases/watcher-gateway/test-sha"
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "node_modules")
    for relative in CODE_SUBSET:
        shutil.copytree(REPO / relative, code_root / relative, ignore=ignore)
    env = {name: _token(role) for role, name in READER_ENV.items()}
    env["WATCHER_GATEWAY_TOKEN"] = _token("gateway")
    env["WATCHER_GATEWAY_URL"] = "http://127.0.0.1:%d" % _free_port()
    env["CONTROL_PLANE_APP_ROLE"] = "watcher-gateway"
    env["LANG"] = env["LC_ALL"] = "en_US.UTF-8"
    random_token, signal_shaped = _token("random"), _token("signal")
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", CHILD, str(code_root), random_token, signal_shaped],
        env=env, cwd=str(tmp_path), capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    line = next(item for item in result.stdout.splitlines() if item.startswith("RESULT "))
    data = json.loads(line[len("RESULT "):])
    for value in env.values():
        if value.startswith(("system_observer-", "viewer-", "risk_admin-", "reviewer-", "gateway-")):
            assert value not in result.stdout and value not in result.stderr
    assert data["startup"] == "lifespan.startup.complete"
    assert data["shutdown"] == "lifespan.shutdown.complete"
    assert data["role"] == "watcher-gateway" and data["current_role"] == "watcher-gateway"
    assert data["ok"] == [200, {"configured": True}]
    assert data["missing"] == [401, "unauthenticated"]
    assert data["random"] == [403, "invalid_token"]
    assert data["signal"] == [403, "invalid_token"]
    assert data["media_get"] == [200, b"\x89PNG".hex(), "image/png"]
    assert data["media_head"] == [200, "4", 0]
    assert data["health"] == [200, HEALTH_OK]
    assert data["accounts"] == 404
    assert data["upstream"] == [["GET", "/api/status"], ["GET", "/media/1700000000000-1.png"], ["HEAD", "/media/1700000000000-1.png"]]
    assert data["counts_after_requests"] == {"psycopg2.connect": 0, "checkout_role_connection": 0, "_database_connection": 0, "BoundedPostgresPool": 0}
    assert data["active"] == {"checkout_role_connection": "PoolConfigurationError", "_database_connection": "PoolConfigurationError", "BoundedPostgresPool": "PoolConfigurationError"}
    assert data["connects_total"] == 0
    roots = data["roots"]
    assert roots["code_root"] == os.path.realpath(code_root)
    allowed = [roots["code_root"], roots["prefix"], *roots["stdlib"]]
    def inside(path, roots_):
        return any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots_)
    outside = [path for path in data["files"] if not inside(path, allowed)]
    # The only file outside code root / venv / stdlib that may be opened is the
    # OS version plist that CPython's own _osx_support reads on macOS (via
    # sysconfig during the zoneinfo import); nothing else, and only from there.
    for path in outside:
        opener = data["openers"].get(path)
        assert path == "/System/Library/CoreServices/SystemVersion.plist", path
        assert opener is not None and opener.endswith("/_osx_support.py") and inside(opener, roots["stdlib"]), (path, opener)
    repo = os.path.realpath(REPO)
    venv = roots["prefix"].rstrip("/") + "/"
    assert not [path for path in data["files"] if path.startswith(repo + "/") and not path.startswith(venv)]
    loaded_from_root = [path for path in data["files"] if path.startswith(roots["code_root"] + "/")]
    assert any(path.endswith("services/control-plane/api/read_api.py") for path in loaded_from_root)
    assert any(path.endswith("services/control-plane/api/generated/watcher_gateway_routes.py") for path in loaded_from_root)
