"""Async, contract generated watcher gateway for the watcher-gateway role.

WGW-1.0.4 (contracts/backend-api.md §9.14.6): these routes and the prefix
middleware are registered only on ``create_app("watcher-gateway")``. The
module never touches PostgreSQL; authentication uses the environment only.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import logging
import math
import os
import re
from uuid import uuid4

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import compile_path

from security.permissions import AuthRequired, PermissionDenied
from security.principal import PrincipalKind, TokenCatalogError, configured_token_values, resolve_principal

PAYLOAD = None
SECRET_KEY_REGEX = None
_LOAD_ERROR = None

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


def path_part(target):
    return re.split(r"[?#]", target, maxsplit=1)[0]


def ascii_lower(value):
    return re.sub(r"[A-Z]", lambda match: match.group().lower(), value)


def na(path, entries):
    candidate = ascii_lower(path)
    for entry in entries:
        denied = ascii_lower(entry["inner_path"])
        if denied.endswith("/*"):
            base = denied[:-2]
            if candidate == base or candidate.startswith(base + "/"):
                return True
        elif candidate == denied or candidate == denied + "/":
            return True
    return False


def na_intersects(template, entry):
    denied = entry["inner_path"]
    wildcard = denied.endswith("/*")
    expected = (denied[:-2] if wildcard else denied)[1:].split("/")
    actual = template[1:].split("/")
    if len(actual) < len(expected) or (not wildcard and len(actual) != len(expected)):
        return False
    for candidate, segment in zip(actual, expected):
        if re.fullmatch(r"\{[a-z][a-z0-9_]*\}", candidate) and segment:
            continue
        if ascii_lower(candidate) != ascii_lower(segment):
            return False
    return True


def na_gw(path, source=None):
    source = PAYLOAD if source is None else source
    prefix = source["paths"]["app_outer_prefix"]
    entries = []
    for entry in source["never_allowed"]:
        inner = entry["inner_path"]
        outer = prefix + (inner[4:] if inner.startswith("/api/") else inner)
        entries.append({"inner_path": outer})
    return na(path, entries)


def _validate_artifact(source):
    entries = source["never_allowed"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("never_allowed_empty")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"inner_path", "methods", "reason"} or entry["methods"] != "*" or not isinstance(entry["reason"], str) or not entry["reason"]:
            raise ValueError("never_allowed_shape")
        denied = entry["inner_path"]
        if not isinstance(denied, str) or not re.fullmatch(r"/|/(?:[A-Za-z0-9._-]+/)*(?:[A-Za-z0-9._-]+|\*)", denied) or denied in seen:
            raise ValueError("never_allowed_path")
        seen.add(denied)
        if not any(na_intersects(row["inner_path"], entry) for row in source["routes"] if row["identity"] == "browser"):
            raise ValueError("never_allowed_browser_intersection")
    for row in source["routes"]:
        if row["identity"] != "gateway":
            continue
        if any(na_intersects(row["inner_path"], entry) for entry in entries):
            raise ValueError("never_allowed_gateway_intersection")
        if row["budget"] not in source["budgets"]:
            raise ValueError("route_budget")
        _match_template(row["outer_path"], row["outer_path"], source["path_params"])
        compile_path(row["outer_path"])


def load_route_artifact(source=None, loader=None):
    """Reentrant startup loader; a failed artifact disables only watcher routes."""
    global PAYLOAD, SECRET_KEY_REGEX, _LOAD_ERROR
    try:
        if source is None:
            module = (loader or importlib.import_module)("generated.watcher_gateway_routes")
            source = module.PAYLOAD
            secret_regex = module.SECRET_KEY_REGEX
        else:
            flags = re.I if "i" in source["secret_key_pattern"]["flags"] else 0
            secret_regex = re.compile(source["secret_key_pattern"]["pattern"], flags)
        _validate_artifact(source)
        PAYLOAD, SECRET_KEY_REGEX, _LOAD_ERROR = source, secret_regex, None
        return True
    except Exception as exc:
        PAYLOAD, SECRET_KEY_REGEX, _LOAD_ERROR = None, None, type(exc).__name__
        checks = {"never_allowed_empty", "never_allowed_shape", "never_allowed_path", "never_allowed_browser_intersection", "never_allowed_gateway_intersection", "route_budget"}
        check = str(exc) if isinstance(exc, ValueError) and str(exc) in checks else "artifact_load"
        LOG.error("watcher gateway artifact disabled: %s %s", type(exc).__name__, check)
        return False


load_route_artifact()


def gateway_route_names(source=None):
    """Route names of the registered gateway rows, taken from the payload.

    §9.14.6 / §9.16 B-8: ``identity == gateway`` and ``phase <= phase_max``,
    named ``watcher_gateway__<id with . replaced by _>``. Empty when the
    artifact is disabled.
    """
    source = PAYLOAD if source is None else source
    if source is None:
        return frozenset()
    phases = ("P0", "P1", "P2", "P3")
    phase_max = phases.index(source["_meta"]["phase_max"])
    return frozenset(
        "watcher_gateway__" + row["id"].replace(".", "_")
        for row in source["routes"]
        if row["identity"] == "gateway" and phases.index(row["phase"]) <= phase_max
    )


def readiness():
    """(ready, reason) for G5 and the role health check (§9.3, §9.14.6).

    Not ready when the artifact is disabled (R11), when
    ``WATCHER_GATEWAY_TOKEN`` is missing, or when it collides with a token
    visible in this process (or the token catalog is unusable). ``reason``
    names variables only and never contains a value.
    """
    if _LOAD_ERROR is not None or PAYLOAD is None:
        return False, "watcher gateway artifact disabled"
    token = os.getenv("WATCHER_GATEWAY_TOKEN", "")
    if not token:
        return False, "WATCHER_GATEWAY_TOKEN missing"
    try:
        collision = token in configured_token_values()
    except TokenCatalogError:
        collision = True
    if collision:
        return False, "WATCHER_GATEWAY_TOKEN collision or token catalog unavailable"
    return True, None


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
        decoded_part, raw_part = path_part(decoded), path_part(raw)
        def triggered(path):
            lower = path.lower()
            return lower == PREFIX or lower.startswith(PREFIX + "/") or lower.startswith(PREFIX + "%")
        if not (triggered(decoded_part) or triggered(raw_part)):
            await self.app(scope, receive, send)
            return
        request_id = uuid4().hex
        if _LOAD_ERROR is not None or PAYLOAD is None:
            response = _error(503, "gateway_disabled", request_id)
            await response(scope, receive, send)
            return
        invalid = (not raw.startswith("/") or "#" in raw or "%" in raw_part or "//" in raw_part or raw_part.endswith("/") or any(p in (".", "..") for p in raw_part.split("/")))
        if invalid or na_gw(decoded_part) or na_gw(raw_part):
            response = _error(404, "route_not_found", request_id)
            await response(scope, receive, send)
            return
        matches = [r for r in self.routes if _match_template(r["outer_path"], raw_part, PAYLOAD["path_params"])]
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
            try:
                check_value = int(value)
            except ValueError:
                return None, _error(400, "invalid_query", request_id)
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
        ready, reason = readiness()
        if not ready:
            LOG.warning("watcher gateway disabled: %s", reason)
            return _error(503, "gateway_disabled", request_id)
        token = os.getenv("WATCHER_GATEWAY_TOKEN", "")
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
            if na(path, PAYLOAD["never_allowed"]):
                return _error(404, "route_not_found", request_id)
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
        headers.pop("content-length", None)
        headers["cache-control"] = "no-store"
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
    global PAYLOAD, SECRET_KEY_REGEX, _LOAD_ERROR
    if PAYLOAD is None:
        return
    try:
        for row in PAYLOAD["routes"]:
            if row["identity"] != "gateway":
                continue
            def make_endpoint(route):
                async def endpoint(request: Request):
                    return await gateway.handle(request, route)
                return endpoint
            endpoint = make_endpoint(row)
            app.add_api_route(row["outer_path"], endpoint, methods=[row["method"]], name="watcher_gateway__" + row["id"].replace(".", "_"))
    except Exception as exc:
        PAYLOAD, SECRET_KEY_REGEX, _LOAD_ERROR = None, None, type(exc).__name__
        LOG.error("watcher gateway artifact disabled: %s route_registration", type(exc).__name__)


def install_middleware(app):
    if any(item.cls is GatewayPathMiddleware for item in app.user_middleware):
        return
    app.add_middleware(GatewayPathMiddleware)
