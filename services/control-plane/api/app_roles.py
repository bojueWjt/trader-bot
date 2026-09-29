from __future__ import annotations

import os
from contextvars import ContextVar, Token
from enum import Enum
from typing import Iterable

from fastapi import FastAPI
from fastapi.routing import APIRoute


class AppRole(str, Enum):
    ALL = "all"
    NODE_CONTROL = "node-control"
    EVENT_INGEST = "event-ingest"
    OPERATOR_QUERY = "operator-query"
    WATCHER_GATEWAY = "watcher-gateway"


_ACTIVE_APP_ROLE: ContextVar[AppRole | None] = ContextVar(
    "control_plane_app_role",
    default=None,
)

_NODE_CONTROL_ROUTES = frozenset(
    {
        "ack_node_command",
        "ack_node_intent",
        "account_generated_at",
        "node_commands",
        "node_exchange_state",
        "node_heartbeat",
        "node_intents",
        "node_orders",
        "report_node_incident",
        "resolve_node_incident",
    }
)
_EVENT_INGEST_ROUTES = frozenset(
    {
        "ingest_execution_event",
        "post_node_events",
    }
)
_SHARED_ROUTES = frozenset({"role_database_health"})
# contracts/backend-api.md §9.4 / §9.14.6: gateway route names are
# "watcher_gateway__<id>" and exist only on the watcher-gateway role app.
WATCHER_GATEWAY_ROUTE_PREFIX = "watcher_gateway__"
_DATABASE_ROLE_NAMES = {
    AppRole.NODE_CONTROL: "trader_v3_node_control",
    AppRole.EVENT_INGEST: "trader_v3_event_ingest",
    AppRole.OPERATOR_QUERY: "trader_v3_operator_query",
}
_ROLLBACK_ONLY_PERMISSION_PROBES = {
    AppRole.NODE_CONTROL: (
        "UPDATE execution_events SET payload=payload WHERE false"
    ),
    AppRole.EVENT_INGEST: (
        "UPDATE node_heartbeats SET status=status WHERE false"
    ),
    AppRole.OPERATOR_QUERY: (
        "UPDATE node_heartbeats SET status=status WHERE false"
    ),
}


def resolve_app_role(value: AppRole | str | None = None) -> AppRole:
    candidate = value
    if candidate is None:
        candidate = os.environ.get("CONTROL_PLANE_APP_ROLE", AppRole.ALL.value)
    if isinstance(candidate, AppRole):
        return candidate
    normalized = str(candidate or "").strip().lower().replace("_", "-")
    try:
        return AppRole(normalized)
    except ValueError as exc:
        allowed = ", ".join(role.value for role in AppRole)
        raise RuntimeError(
            f"CONTROL_PLANE_APP_ROLE must be one of: {allowed}"
        ) from exc


def current_app_role() -> AppRole:
    active = _ACTIVE_APP_ROLE.get()
    if active is not None:
        return active
    return resolve_app_role()


def bind_app_role(role: AppRole | str) -> Token:
    return _ACTIVE_APP_ROLE.set(resolve_app_role(role))


def reset_app_role(token: Token) -> None:
    _ACTIVE_APP_ROLE.reset(token)


def database_role_name(role: AppRole | str) -> str | bool:
    resolved = resolve_app_role(role)
    return _DATABASE_ROLE_NAMES.get(resolved, False)


def rollback_only_permission_probe(role: AppRole | str) -> str | bool:
    resolved = resolve_app_role(role)
    return _ROLLBACK_ONLY_PERMISSION_PROBES.get(resolved, False)


def expected_database_role_name(role: AppRole | str) -> str:
    resolved = resolve_app_role(role)
    expected = database_role_name(resolved)
    if expected is False:
        raise RuntimeError(
            f"{resolved.value} does not have an isolated database role"
        )
    configured = os.environ.get(
        "CONTROL_PLANE_EXPECT_DATABASE_ROLE",
        "",
    ).strip()
    if configured and configured != expected:
        raise RuntimeError(
            "CONTROL_PLANE_EXPECT_DATABASE_ROLE does not match "
            f"{resolved.value}"
        )
    return expected


def route_names_for_role(
    routes: Iterable[APIRoute],
    role: AppRole | str,
) -> frozenset[str]:
    resolved = resolve_app_role(role)
    application_names = {
        str(route.name)
        for route in routes
        if isinstance(route, APIRoute)
    }
    if resolved is AppRole.ALL:
        return frozenset(application_names)
    if resolved is AppRole.NODE_CONTROL:
        return _NODE_CONTROL_ROUTES | _SHARED_ROUTES
    if resolved is AppRole.EVENT_INGEST:
        return _EVENT_INGEST_ROUTES | _SHARED_ROUTES
    if resolved is AppRole.WATCHER_GATEWAY:
        # The shared app carries no gateway routes (§9.14.6); the gateway
        # names come from the generated payload, never from ``routes``.
        import watcher_gateway

        return frozenset(watcher_gateway.gateway_route_names()) | _SHARED_ROUTES
    owned_elsewhere = _NODE_CONTROL_ROUTES | _EVENT_INGEST_ROUTES
    # Belt and braces (§9.16 B-8): operator-query never serves the gateway.
    gateway_names = {
        name
        for name in application_names
        if name.startswith(WATCHER_GATEWAY_ROUTE_PREFIX)
    }
    return frozenset(
        (application_names - owned_elsewhere - gateway_names) | _SHARED_ROUTES
    )


def build_role_app(
    source_app: FastAPI,
    role: AppRole | str,
) -> FastAPI:
    resolved = resolve_app_role(role)
    if resolved is AppRole.ALL:
        return source_app

    role_app = FastAPI(
        title=source_app.title,
        description=source_app.description,
        version=source_app.version,
        docs_url=source_app.docs_url,
        redoc_url=source_app.redoc_url,
        openapi_url=source_app.openapi_url,
    )
    allowed_names = route_names_for_role(source_app.routes, resolved)
    for route in source_app.routes:
        if not isinstance(route, APIRoute):
            continue
        if str(route.name) not in allowed_names:
            continue
        role_app.router.routes.append(route)

    role_app.state.control_plane_role = resolved.value

    @role_app.middleware("http")
    async def bind_request_role(request, call_next):
        token = bind_app_role(resolved)
        try:
            return await call_next(request)
        finally:
            reset_app_role(token)

    return role_app


class BindRequestRoleMiddleware:
    """Pure ASGI twin of ``build_role_app``'s ``bind_request_role``.

    Binds ``current_app_role()`` to a fixed role for the whole request so
    that ``/health/role`` answers for the watcher-gateway role even when
    ``CONTROL_PLANE_APP_ROLE`` is unset (§9.14.6, §9.16 B-9/B-10 (b)).
    It is a plain ASGI middleware so that streamed media responses are not
    re-wrapped by ``BaseHTTPMiddleware``.
    """

    def __init__(self, app, role: AppRole | str) -> None:
        self.app = app
        self.role = resolve_app_role(role)

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        token = bind_app_role(self.role)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_app_role(token)


def build_watcher_gateway_app(source_app: FastAPI) -> FastAPI:
    """Fresh app for the watcher-gateway role (§9.14.6, §9.16 B-9).

    It carries only the shared health route taken from ``source_app``;
    the caller registers the generated gateway routes, the prefix
    middleware and nothing else (no DB startup check, no snapshot hooks,
    no DB retry handler). docs, redoc and openapi are disabled.
    """
    role_app = FastAPI(
        title=source_app.title,
        description=source_app.description,
        version=source_app.version,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    for route in source_app.routes:
        if isinstance(route, APIRoute) and str(route.name) in _SHARED_ROUTES:
            role_app.router.routes.append(route)
    role_app.state.control_plane_role = AppRole.WATCHER_GATEWAY.value
    return role_app
