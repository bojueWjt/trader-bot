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
    owned_elsewhere = _NODE_CONTROL_ROUTES | _EVENT_INGEST_ROUTES
    return frozenset(
        (application_names - owned_elsewhere) | _SHARED_ROUTES
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
