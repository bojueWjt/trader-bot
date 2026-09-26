"""Async, contract generated watcher gateway for operator-query."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
from uuid import uuid4

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from security.permissions import AuthRequired, PermissionDenied
from security.principal import PrincipalKind, TokenCatalogError, configured_token_values, resolve_principal

try:
    from generated.watcher_gateway_routes import PAYLOAD, SECRET_KEY_REGEX
    _LOAD_ERROR = None
except Exception as exc:  # missing or corrupt generated module disables only this gateway
    PAYLOAD = None
    SECRET_KEY_REGEX = None
    _LOAD_ERROR = exc
    logging.getLogger(__name__).error("watcher gateway route artifact unavailable: %s", type(exc).__name__)

LOG = logging.getLogger(__name__)
PREFIX = "/v1/watcher"
MESSAGES = {
    "route_not_found": "route not found", "method_not_allowed": "method not allowed",
    "unauthenticated": "bearer token required", "invalid_token": "invalid token",
    "insufficient_scope": "insufficient scope", "auth_unavailable": "authentication unavailable",
    "invalid_query": "invalid query", "invalid_body": "invalid body",
    "secret_field_rejected": "secret field rejected", "payload_too_large": "payload too large",
    "gateway_disabled": "watcher gateway disabled", "gateway_busy": "watcher gateway busy",
    "watcher_unavailable": "watcher unavailable", "range_not_satisfiable": "range not satisfiable",
    "media_too_large": "media too large",
}
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
RANGE = re.compile(r"^bytes=(?:([0-9]+)-([0-9]*)|-([0-9]+))$")
CONTENT_RANGE = re.compile(r"^bytes ([0-9]+)-([0-9]+)/([0-9]+)$")


def _error(status, code, request_id=None, details=None, headers=None):
    body = {"code": code, "message": MESSAGES.get(code, code), "request_id": request_id or uuid4().hex}
    if details:
        body["details"] = details
    return JSONResponse(body, status_code=status, headers=headers)


def _secret(key):
    return not key.isascii() or bool(SECRET_KEY_REGEX.search(key))


def _filtered(value, omit=frozenset()):
    if isinstance(value, list):
        return [_filtered(item, omit) for item in value]
    if isinstance(value, dict):
        return {key: _filtered(item, omit) for key, item in value.items() if isinstance(key, str) and key not in omit and not _secret(key)}
    return value


def _match_template(template, path, patterns):
    source = ""
    position = 0
    for match in re.finditer(r"\{([^{}]+)\}", template):
        source += re.escape(template[position:match.start()])
        name = match.group(1)
        pattern = patterns[name]["gateway_pattern"]
        source += "(" + pattern[1:-1] + ")"
        position = match.end()
    source += re.escape(template[position:])
    return re.fullmatch(source, path) is not None


class GatewayPathMiddleware:
    """G1: raw-path gate scoped to the watcher prefix only."""

    def __init__(self, app):
        self.app = app
        self.routes = [] if PAYLOAD is None else [r for r in PAYLOAD["routes"] if r["identity"] == "gateway"]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        decoded = scope.get("path", "")
        raw = scope.get("raw_path", decoded.encode("latin1", "ignore")).decode("latin1")
        def triggered(path):
            lower = path.lower()
            return lower == PREFIX or lower.startswith(PREFIX + "/") or lower.startswith(PREFIX + "%")
        if not (triggered(decoded) or triggered(raw)):
            await self.app(scope, receive, send)
            return
        request_id = uuid4().hex
        if PAYLOAD is None:
            response = _error(503, "gateway_disabled", request_id)
            await response(scope, receive, send)
            return
        invalid = "%" in raw or "//" in raw or raw.endswith("/") or any(p in (".", "..") for p in raw.split("/"))
        matches = [] if invalid else [r for r in self.routes if _match_template(r["outer_path"], raw, PAYLOAD["path_params"])]
        if not matches:
            response = _error(404, "route_not_found", request_id)
            await response(scope, receive, send)
            return
        row = next((r for r in matches if r["method"] == scope["method"]), None)
        if row is None:
            allow = ", ".join(sorted({r["method"] for r in matches}))
            response = _error(405, "method_not_allowed", request_id, headers={"Allow": allow})
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})["watcher_gateway_route_id"] = row["id"]
        scope["state"]["watcher_gateway_request_id"] = request_id
        await self.app(scope, receive, send)


class MediaStreamingResponse(StreamingResponse):
    def __init__(self, *args, upstream, semaphore, **kwargs):
        super().__init__(*args, **kwargs)
        self._upstream = upstream
        self._semaphore = semaphore

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._upstream.aclose()
            self._semaphore.release()


def _value_valid(value, definition, gateway=True):
    kind = definition["type"]
    if kind == "string":
        if not isinstance(value, str):
            return "type"
        if "min_length" in definition and len(value) < definition["min_length"]:
            return "min_length"
        if "max_length" in definition and len(value) > definition["max_length"]:
            return "max_length"
        choices = definition.get("gateway_enum" if gateway and "gateway_enum" in definition else "enum")
        if choices is not None and value not in choices:
            return "enum"
        pattern = definition.get("gateway_pattern" if gateway and "gateway_pattern" in definition else "pattern")
        if pattern is not None and re.fullmatch(pattern, value) is None:
            return "pattern"
    elif kind == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            return "type"
    elif kind == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            return "type"
    elif kind == "boolean":
        if not isinstance(value, bool):
            return "type"
    elif kind == "array":
        if not isinstance(value, list):
            return "type"
        if "max_items" in definition and len(value) > definition["max_items"]:
            return "max_items"
        if definition.get("unique") and len(set(map(str, value))) != len(value):
            return "unique"
        item_def = PAYLOAD["body_fields"][definition["items"]]
        if any(_value_valid(item, item_def) is not None for item in value):
            return "items"
    else:
        return "type"
    for rule in ("min", "max", "exclusive_min"):
        if rule in definition and kind in ("integer", "number"):
            bound = definition[rule]
            if rule == "min" and value < bound or rule == "max" and value > bound or rule == "exclusive_min" and value <= bound:
                return rule
    return None


async def _body(request, row, request_id):
    max_bytes = PAYLOAD["budgets"]["config"]["max_request_body_bytes"]
    length = request.headers.get("content-length")
    if length:
        try:
            if row["method"] in ("GET", "HEAD") and int(length) > 0:
                return None, _error(400, "invalid_body", request_id)
            if int(length) > max_bytes:
                return None, _error(413, "payload_too_large", request_id)
        except ValueError:
            return None, _error(400, "invalid_body", request_id)
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > max_bytes:
            return None, _error(413, "payload_too_large", request_id)
    if row["method"] in ("GET", "HEAD"):
        if content:
            return None, _error(400, "invalid_body", request_id)
        return None, None
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        return None, _error(400, "invalid_body", request_id)
    try:
        value = json.loads(content)
    except (ValueError, UnicodeDecodeError):
        return None, _error(400, "invalid_body", request_id)
    if not isinstance(value, dict):
        return None, _error(400, "invalid_body", request_id)
    secrets = sorted(k for k in value if not isinstance(k, str) or _secret(k) or k in PAYLOAD["secret_fields"])
    if secrets:
        return None, _error(400, "secret_field_rejected", request_id, {"fields": secrets})
    allow = set(row["body"]["allow"])
    unknown = sorted(set(value) - allow)
    if unknown:
        return None, _error(400, "invalid_body", request_id, {"unknown_fields": unknown})
    missing = sorted(set(row["body"]["required"]) - set(value))
    if missing:
        return None, _error(400, "invalid_body", request_id, {"missing_fields": missing})
    for field, item in value.items():
        definition = PAYLOAD["body_fields"][row["body"].get("field_overrides", {}).get(field, field)]
        invalid = _value_valid(item, definition)
        if invalid:
            return None, _error(400, "invalid_body", request_id, {"field": field, "rule": invalid})
    return json.dumps(value, separators=(",", ":")).encode(), None


def _query(request, row, request_id):
    seen = set()
    values = []
    for key, value in request.query_params.multi_items():
        if key in seen or key not in row["query"] or _secret(key):
            return None, _error(400, "invalid_query", request_id)
        seen.add(key)
        definition = PAYLOAD["query_params"][key]
        if definition["type"] == "integer":
            if re.fullmatch(r"[0-9]+", value) is None:
                return None, _error(400, "invalid_query", request_id)
            check_value = int(value)
        else:
            check_value = value
        if _value_valid(check_value, definition) is not None:
            return None, _error(400, "invalid_query", request_id)
        values.append((key, value))
    for key in seen:
        other = PAYLOAD["query_params"][key].get("requires")
        if other and other not in seen:
            return None, _error(400, "invalid_query", request_id)
    return values, None


def _range_valid(value):
    match = RANGE.fullmatch(value)
    if match is None:
        return False
    start, end, suffix = match.groups()
    if suffix is not None:
        return int(suffix) > 0
    return not end or int(start) <= int(end)


class Gateway:
    def __init__(self):
        self._sem = {}
        self._clients = {}
        self._loops = {}

    def _resources(self, budget):
        loop = asyncio.get_running_loop()
        if self._loops.get(budget) is not loop:
            config = PAYLOAD["budgets"][budget]
            configured = os.getenv(config["env"], config["slots_per_worker"]) if config["env"] else config["slots_per_worker"]
            slots = int(configured)
            slots = max(1, slots)
            self._sem[budget] = asyncio.Semaphore(slots)
            timeout = config["timeouts_s"]
            self._clients[budget] = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=timeout["connect"], read=timeout["read"], write=timeout["write"], pool=timeout["pool"]),
                limits=httpx.Limits(max_connections=slots, max_keepalive_connections=slots),
                follow_redirects=False,
            )
            self._loops[budget] = loop
        return self._sem[budget], self._clients[budget]

    async def handle(self, request, row):
        request_id = request.scope.get("state", {}).get("watcher_gateway_request_id", uuid4().hex)
        if request.scope.get("state", {}).get("watcher_gateway_route_id") != row["id"]:
            return _error(404, "route_not_found", request_id)
        try:
            principal = resolve_principal(request.headers.get("authorization"))
        except AuthRequired:
            return _error(401, "unauthenticated", request_id)
        except PermissionDenied:
            return _error(403, "invalid_token", request_id)
        except TokenCatalogError:
            return _error(503, "auth_unavailable", request_id)
        if principal.kind is PrincipalKind.SIGNAL or principal.role not in row["roles"]:
            return _error(403, "insufficient_scope", request_id)
        query, error = _query(request, row, request_id)
        if error:
            return error
        content, error = await _body(request, row, request_id)
        if error:
            return error
        token = os.getenv("WATCHER_GATEWAY_TOKEN", "")
        try:
            collision = token and token in configured_token_values()
        except TokenCatalogError:
            collision = True
        if not token or collision:
            return _error(503, "gateway_disabled", request_id)
        budget = row["budget"]
        try:
            sem, client = self._resources(budget)
        except (ValueError, TypeError):
            return _error(503, "gateway_disabled", request_id)
        if sem.locked():
            return _error(503, "gateway_busy", request_id)
        await sem.acquire()
        release = True
        upstream = None
        try:
            normalized = request.headers.get("authorization", "")[len("Bearer "):].strip()
            if not normalized or not principal.role:
                return _error(503, "watcher_unavailable", request_id, {"reason": "actor_injection_failed"})
            fingerprint = hashlib.sha256(normalized.encode()).hexdigest()[:12]
            headers = {
                "Authorization": "Bearer " + token,
                "X-Watcher-Actor": "app:" + principal.role,
                "X-Watcher-Token-Fingerprint": fingerprint,
                "Accept": "application/json",
            }
            if content is not None:
                headers["Content-Type"] = "application/json"
            if budget == "media":
                if row["method"] == "GET":
                    raw_headers = [name.lower() for name, _ in request.scope.get("headers", ())]
                    if raw_headers.count(b"range") > 1 or raw_headers.count(b"if-range") > 1:
                        return _error(416, "range_not_satisfiable", request_id)
                    range_header = request.headers.get("range")
                    if range_header:
                        if not _range_valid(range_header):
                            return _error(416, "range_not_satisfiable", request_id)
                        headers["Range"] = range_header
                    if request.headers.get("if-range"):
                        headers["If-Range"] = request.headers["if-range"]
            upstream_url = os.getenv("WATCHER_GATEWAY_URL", PAYLOAD["paths"]["watcher_upstream_default"]).rstrip("/")
            if not re.fullmatch(r"https?://[^/?#]+", upstream_url):
                return _error(503, "gateway_disabled", request_id)
            path = row["inner_path"]
            for key, value in request.path_params.items():
                path = path.replace("{" + key + "}", value)
            req = httpx.Request(row["method"], upstream_url + path, params=query, content=content, headers=headers)
            timeout = PAYLOAD["budgets"][budget]["timeouts_s"]["total"]
            deadline = asyncio.get_running_loop().time() + timeout
            async with asyncio.timeout_at(deadline):
                upstream = await client.send(req, stream=True, follow_redirects=False)
                if budget == "media" and upstream.status_code in (200, 206):
                    response = self._media(upstream, row, request.path_params["filename"], request_id, sem, deadline)
                    if isinstance(response, MediaStreamingResponse):
                        release = False
                        upstream = None
                    return response
                response = await self._json(upstream, row, request_id)
                return response
        except (asyncio.TimeoutError, httpx.TimeoutException):
            return _error(503, "watcher_unavailable", request_id, {"reason": "timeout"})
        except (httpx.ConnectError, httpx.NetworkError):
            return _error(503, "watcher_unavailable", request_id, {"reason": "connect_error"})
        except (httpx.HTTPError, ValueError):
            return _error(503, "watcher_unavailable", request_id, {"reason": "malformed_response"})
        finally:
            if upstream is not None:
                await upstream.aclose()
            if release:
                sem.release()

    async def _json(self, upstream, row, request_id):
        status = upstream.status_code
        if 300 <= status < 400:
            return _error(503, "watcher_unavailable", request_id, {"reason": "redirect", "upstream_status": status})
        if status not in (200, 400, 404, 409, 416, 503):
            return _error(503, "watcher_unavailable", request_id, {"reason": "upstream_status", "upstream_status": status})
        max_bytes = PAYLOAD["budgets"]["config"]["max_response_body_bytes"]
        body = bytearray()
        async for chunk in upstream.aiter_bytes():
            body.extend(chunk)
            if len(body) > max_bytes:
                return _error(503, "watcher_unavailable", request_id, {"reason": "response_too_large"})
        try:
            parsed = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return _error(503, "watcher_unavailable", request_id, {"reason": "malformed_response"})
        if status != 200:
            if not isinstance(parsed, dict) or not isinstance(parsed.get("code"), str) or ERROR_CODE.fullmatch(parsed["code"]) is None:
                return _error(503, "watcher_unavailable", request_id, {"reason": "malformed_response"})
            code = parsed["code"]
            if status == 503 and code not in ("db_busy", "media_too_large") or status == 400 and code == "invalid_actor_headers" or status == 404 and code == "route_not_found":
                return _error(503, "watcher_unavailable", request_id, {"reason": "upstream_status", "upstream_status": status})
            message = parsed.get("message")
            if not isinstance(message, str) or len(message) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in message):
                message = code
            result = {"code": code, "message": message, "request_id": request_id}
            if isinstance(parsed.get("details"), dict):
                result["details"] = _filtered(parsed["details"])
        else:
            result = _filtered(parsed, frozenset(row["response"]["omit"]))
        headers = self._response_headers(upstream, row)
        headers["Cache-Control"] = "no-store"
        return JSONResponse(result, status_code=status, headers=headers)

    def _response_headers(self, upstream, row):
        permitted = {s.lower() for s in PAYLOAD["response_headers"][row["response"]["headers"]]}
        hop = {s.strip().lower() for s in upstream.headers.get("connection", "").split(",")}
        return {key.lower(): value for key, value in upstream.headers.items() if key.lower() in permitted and key.lower() not in hop}

    def _media(self, upstream, row, filename, request_id, sem, deadline):
        status = upstream.status_code
        max_file = PAYLOAD["budgets"]["media"]["max_file_bytes"]
        expected_length = None
        if status == 200:
            raw_total = upstream.headers.get("content-length", "")
        else:
            match = CONTENT_RANGE.fullmatch(upstream.headers.get("content-range", ""))
            raw_total = match.group(3) if match else ""
            if match:
                start, end, total_value = (int(value) for value in match.groups())
                if start > end or end >= total_value:
                    return _error(503, "watcher_unavailable", request_id, {"reason": "malformed_response"})
                expected_length = end - start + 1
        if not raw_total.isdigit():
            return _error(503, "watcher_unavailable", request_id, {"reason": "malformed_response"})
        total = int(raw_total)
        if total > max_file:
            return _error(503, "media_too_large", request_id)
        if status == 206 and row["method"] == "HEAD":
            return _error(503, "watcher_unavailable", request_id, {"reason": "malformed_response"})
        headers = self._response_headers(upstream, row)
        headers["cache-control"] = "private, no-store"
        content_type = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}[filename.rsplit(".", 1)[-1]]
        headers["content-type"] = content_type
        declared_length = upstream.headers.get("content-length", "")
        if row["method"] == "GET":
            if status == 200:
                expected_length = total
            if not declared_length.isdigit() or int(declared_length) != expected_length:
                return _error(503, "watcher_unavailable", request_id, {"reason": "malformed_response"})
        if row["method"] == "HEAD":
            headers["content-length"] = str(total)
            return Response(status_code=200, headers=headers)
        max_chunk = PAYLOAD["budgets"]["media"]["max_chunk_bytes"]
        async def stream():
            sent = 0
            reason = None
            try:
                async with asyncio.timeout_at(deadline):
                    async for raw_chunk in upstream.aiter_bytes():
                        for offset in range(0, len(raw_chunk), max_chunk):
                            chunk = raw_chunk[offset:offset + max_chunk]
                            if sent + len(chunk) > max_file or sent + len(chunk) > expected_length:
                                reason = "size" if sent + len(chunk) > max_file else "length"
                                break
                            sent += len(chunk)
                            yield chunk
                        if reason:
                            break
                    if sent != expected_length:
                        reason = "short_body"
            except (asyncio.CancelledError, asyncio.TimeoutError, httpx.HTTPError) as exc:
                reason = type(exc).__name__
                if isinstance(exc, asyncio.CancelledError):
                    raise
            finally:
                if reason:
                    LOG.warning("truncated request_id=%s filename=%s bytes_sent=%d reason=%s", request_id, filename, sent, reason)
        return MediaStreamingResponse(stream(), status_code=status, headers=headers, upstream=upstream, semaphore=sem)


gateway = Gateway()


def register_routes(app):
    if PAYLOAD is None:
        return
    for row in PAYLOAD["routes"]:
        if row["identity"] != "gateway":
            continue
        def make_endpoint(route):
            async def endpoint(request: Request):
                return await gateway.handle(request, route)
            return endpoint
        endpoint = make_endpoint(row)
        app.add_api_route(row["outer_path"], endpoint, methods=[row["method"]], name="watcher_gateway__" + row["id"].replace(".", "_"))


def install_middleware(app):
    app.add_middleware(GatewayPathMiddleware)
