#!/usr/bin/env python3
"""O-0 Caddy helper for the WGW-1.0.2 watcher-gateway snippet: check artifacts, verify adapted JSON, probe a local Caddy.

DRAFT for docs/agent-team/release/o0-runbook-deploy.md (stage C) and
o0-site-checklist.md (S-02 .. S-04, L-A1).

Contract: contracts/backend-api.md §9.14.3 (Caddy list format v2, snippet
contracts/generated/caddy-watcher-gateway.caddy, O-0 merge rules) and §9.16 F-04, F-10,
F-12, F-13. User ruling 2026-09-26 (F-04): ``{param}`` = EXACTLY ONE NON-EMPTY SEGMENT,
anchored, case-sensitive ``path_regexp`` ``[^/]+``; the fallback
``^(?i:/m/v1/watcher)(?:[/\\n]|$)`` answers 404 for everything else under the prefix.

There is NO render step (wac-060): the Caddyfile imports the COMMITTED snippet file as is.
This tool only proves that what Caddy will run equals that snippet and the list.

Sub-commands
  check-artifacts  list (format v2) + snippet (format snippet.v1): headers, ``_format``, every
             list row re-derived from its template, the snippet re-rendered here from the list
             and compared byte for byte (an independent oracle; it does not import the
             contract generator).                                               [package G3]
  caddyfile-check  text checks on a candidate Caddyfile (F-12): the snippet FILE is imported
             once at the top level, ``import watcher_gateway_routes`` is in a site block
             before every handle/handle_path/route; prints the F-13 (2) manual record
             (global ``order`` options, top-level pre-handle directives) with every argument
             passed through o0_tool.redact_line.
  verify     on the JSON of ``caddy adapt`` (or the admin API):
             (a) every ``path_regexp`` starting ``^/m/v1/watcher/`` as (pattern, methods) equals
                 the list's (regex, methods): symmetric difference empty, rows > 0;
             (b) the fallback exists once, equals the snippet's pattern verbatim, has no other
                 matcher, answers 404 only, and is the LAST of the contiguous wgw routes;
             (c) F-13 shadow check, two steps, on every route that runs before the first wgw
                 route (same list and every enclosing list, plus handlers that precede the
                 enclosing subroute): does it hit /m/v1/watcher, /m/v1/watcher/status,
                 /M/V1/WATCHER/dialogs (path: Caddy semantics, path_regexp: RE2 semantics,
                 anything else counts as a hit)?  If it hits, every handler must be one of
                 encode, headers WITHOUT "request", vars, map, log_append, tracing, and the
                 route must carry neither "terminal" nor "group";
             (d) no other route that may see the prefix (incl. /m/v1/watcherx, "\\r", "#")
                 proxies to operator-query or the watcher (§9.14.3 merge rule, F-10 coverage);
             (e) emulated Caddy route selection per list line and method: exactly one ``/m``
                 strip, upstream 127.0.0.1:8183, Authorization and X-Watcher-* untouched, no
                 basic auth; §9.14.4 item 1 negatives never reach the gateway; the fallback
                 probes end in the fallback 404; browser samples that reach the watcher pass
                 basic auth, clear X-Watcher-* and Authorization, inject
                 ``{env.WATCHER_BROWSER_PROXY_TOKEN}``.
             Anything the emulator cannot evaluate is reported UNCOMPARABLE and fails.
  inventory  structural summary of every route (no header values, no hashes).
  probe      local (NON-production) Caddy probe on a COPY of the production Caddyfile (F-12,
             F-13 (3)): writes <copy>.o0probe next to the copy (site address -> 127.0.0.1,
             upstreams -> local stubs, admin off, auto_https off, free http/https ports; the
             line diff is recorded), adapts + verifies it, runs it and sends raw request lines.
             Literal dot segments and ``//`` are expected to be CLEANED and forwarded (never
             expected as 404); percent-encoded forms and ``#`` are forwarded raw (``#`` as %23).
  selftest   fixture-based self-test shaped like real ``caddy adapt`` output (good config +
             broken variants, raw and skeleton), artifact parser negatives, RE2 pins.

The adapted JSON contains the basic-auth bcrypt hash: keep it 0600 and never print it.
This tool never prints header values except ``{placeholder}`` names.
"""

from __future__ import annotations

import argparse
import copy
import difflib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

MOBILE_PREFIX = "/m"
APP_OUTER_PREFIX = "/v1/watcher"
EXTERNAL_PREFIX = MOBILE_PREFIX + APP_OUTER_PREFIX          # /m/v1/watcher
WATCHER_PREFIX = EXTERNAL_PREFIX + "/"
LIST_FORMAT = "watcher-gateway-caddy-paths.v2"
SNIPPET_FORMAT = "watcher-gateway-caddy-snippet.v1"
SNIPPET_FILE = "caddy-watcher-gateway.caddy"
SNIPPET_NAME = "watcher_gateway_routes"
UPSTREAM_SNIPPET = "watcher_gateway_upstream"
# §9.14.3, written out independently of the generator (the "\n" is the two characters \ and n)
FALLBACK_REGEX = "^(?i:" + EXTERNAL_PREFIX + ")(?:[/\\n]|$)"
FALLBACK_NAME = "wgw_fallback"
HEADER_KEYS = ("_generated_from", "_yaml_sha256", "_phase_max", "_format")
HTTP_METHODS = ("DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT")
LITERAL_SEGMENT = re.compile(r"^[a-z0-9-]+$")
PARAM_SEGMENT = re.compile(r"^\{[a-z][a-z0-9_]*\}$")
DEFAULT_GATEWAY_UPSTREAM = "127.0.0.1:8183"
DEFAULT_WATCHER_UPSTREAM = "127.0.0.1:9090"
WATCHER_PORTS = ("9090", "9100")
BROWSER_TOKEN_PLACEHOLDER = "{env.WATCHER_BROWSER_PROXY_TOKEN}"
RESPONDERS = {"reverse_proxy", "static_response", "file_server", "error", "php_fastcgi", "copy_response"}
EVALUABLE = {"host", "path", "path_regexp", "method", "not", "protocol"}
# F-13 (b): the three shadow probes and the handler whitelist (only a contract amendment may extend it)
SHADOW_PROBES = ("/m/v1/watcher", "/m/v1/watcher/status", "/M/V1/WATCHER/dialogs")
SHADOW_WHITELIST = ("encode", "headers", "vars", "map", "log_append", "tracing")
# §9.14.3 merge rule + F-10 coverage: nothing but the snippet may forward these to operator-query/watcher
PREFIX_PROBES = SHADOW_PROBES + ("/m/v1/watcherx", "/m/v1/watcher\r", "/m/v1/watcher#x", "/M/V1/Watcher/status")
# §9.14.4 item 1, fallback probes (RE2 truth, verified on Go regexp by the contract author)
FALLBACK_MATCH = ("/m/v1/watcher", "/m/v1/watcher/", "/M/V1/WATCHER/login/start", "/m/v1/watcher/login/start",
                  "/m/v1/watcher\n", "/M/V1/WATCHER\n", "/m/v1/watcher/status\n", "/m/v1/watcher/status#x")
FALLBACK_NO_MATCH = ("/m/v1/watcherx", "/m/v1/other", "/m/v1/watcherx\n", "/m/v1/watcher\r", "/m/v1/watcher#x")
# Caddy v2.10.2 default directive order: the directives that run BEFORE handle (directives.go:47-84)
PRE_HANDLE_DIRECTIVES = ("tracing", "map", "vars", "fs", "root", "log_append", "skip_log", "log_skip", "log_name", "header",
                         "request_body", "redir", "method", "rewrite", "uri", "try_files", "basicauth", "basic_auth",
                         "forward_auth", "request_header", "encode", "push", "intercept", "templates", "invoke")
DEFAULT_MOBILE_SAMPLES = ("GET /m/v1/accounts", "GET /m/v1/mirror/positions", "POST /m/v1/operator/orders")
DEFAULT_BROWSER_SAMPLES = (
    "GET /watcher/",
    "GET /api/status",
    "GET /api/trading/accounts",
    "GET /api/config",
    "POST /api/login/start",
    "GET /media/1700000000000-1.jpg",
    "GET /healthz",
)


class Uncomparable(Exception):
    pass


class ArtifactError(Exception):
    pass


# ---------------------------------------------------------------- artifacts
def _is_param(segment: str) -> bool:
    return PARAM_SEGMENT.match(segment) is not None


def expected_regex(template: str) -> str:
    """§9.14.3 regex(template): literal segments as is, {param} -> [^/]+, anchored both ends."""
    return "^" + "/".join("[^/]+" if _is_param(s) else s for s in template.split("/")) + "$"


@dataclass(frozen=True)
class Line:
    template: str
    regex: str
    methods: tuple[str, ...]

    @property
    def segments(self) -> list[str]:
        return self.template[len(WATCHER_PREFIX):].split("/")

    @property
    def matcher_name(self) -> str:
        return "wgw_r_" + "_".join(s.strip("{}").replace("-", "_") for s in self.segments)

    @property
    def has_param(self) -> bool:
        return any(_is_param(s) for s in self.segments)

    @property
    def last_is_param(self) -> bool:
        return _is_param(self.segments[-1])

    def sample(self, fill: str | None = None, only: int | None = None) -> str:
        """Template with every {param} (or only the ``only``-th one) replaced; the others get 'x1'."""
        out, n = [], 0
        for seg in self.template.split("/"):
            if _is_param(seg):
                default = "1700000000000-1.jpg" if seg == "{filename}" else "x1"
                use = fill if (fill is not None and (only is None or only == n)) else default
                out.append(use)
                n += 1
            else:
                out.append(seg)
        return "/".join(out)

    def param_count(self) -> int:
        return sum(1 for s in self.segments if _is_param(s))


def _read_ascii(path: Path, what: str) -> str:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ArtifactError(f"{what} unreadable: {path} ({type(exc).__name__})") from None
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise ArtifactError(f"{what} is not ASCII") from None
    if "\r" in text or not text.endswith("\n") or text.endswith("\n\n"):
        raise ArtifactError(f"{what} must be LF-only and end with exactly one newline")
    if "*" in text:
        raise ArtifactError(f"{what} contains '*' (WGW-1.0.2: no '*' in either Caddy artifact)")
    return text


def _parse_header(rows: list[str], fmt: str, what: str) -> dict[str, str]:
    if len(rows) < 4:
        raise ArtifactError(f"{what} header shorter than 4 lines")
    meta: dict[str, str] = {}
    for key, row in zip(HEADER_KEYS, rows[:4]):
        prefix = f"# {key} "
        if not row.startswith(prefix) or not row[len(prefix):]:
            raise ArtifactError(f"{what} header line {HEADER_KEYS.index(key) + 1} must be '{prefix}<value>'")
        meta[key] = row[len(prefix):]
    if meta["_format"] != fmt:
        raise ArtifactError(f"{what} _format {meta['_format']!r} != {fmt!r}")
    if meta["_generated_from"] != "contracts/watcher-gateway-routes.yaml":
        raise ArtifactError(f"{what} _generated_from {meta['_generated_from']!r}")
    if not re.fullmatch(r"[0-9a-f]{64}", meta["_yaml_sha256"]):
        raise ArtifactError(f"{what} _yaml_sha256 is not 64 hex digits")
    if not re.fullmatch(r"P[0-9]+", meta["_phase_max"]):
        raise ArtifactError(f"{what} _phase_max {meta['_phase_max']!r}")
    return meta


def load_list(path: Path) -> tuple[list[Line], dict[str, str]]:
    """Parse contracts/generated/caddy-watcher-gateway-paths.txt (format v2)."""
    rows = _read_ascii(path, "list").split("\n")[:-1]
    meta = _parse_header(rows, LIST_FORMAT, "list")
    lines: list[Line] = []
    for n, raw in enumerate(rows[4:], 5):
        parts = raw.split(" ")
        if len(parts) < 3 or "" in parts:
            raise ArtifactError(f"list line {n}: want '<template> <regex> <METHOD>[ <METHOD>...]' separated by single spaces")
        template, regex, methods = parts[0], parts[1], tuple(parts[2:])
        if not template.startswith(WATCHER_PREFIX):
            raise ArtifactError(f"list line {n}: template does not start with {WATCHER_PREFIX}")
        for seg in template[len(WATCHER_PREFIX):].split("/"):
            if not (LITERAL_SEGMENT.match(seg) or _is_param(seg)):
                raise ArtifactError(f"list line {n}: segment {seg!r} is neither [a-z0-9-]+ nor {{name}} (S-22)")
        if regex != expected_regex(template):
            raise ArtifactError(f"list line {n}: regex {regex!r} != {expected_regex(template)!r} derived from the template")
        if list(methods) != sorted(set(methods)) or not set(methods) <= set(HTTP_METHODS):
            raise ArtifactError(f"list line {n}: methods must be unique, sorted, from {','.join(HTTP_METHODS)}")
        if lines and not lines[-1].template.encode() < template.encode():
            raise ArtifactError(f"list line {n}: templates must be unique and in ascending byte order")
        lines.append(Line(template, regex, methods))
    if not lines:
        raise ArtifactError("list has 0 rows (uncomparable)")
    names = [line.matcher_name for line in lines]
    if len(set(names)) != len(names) or FALLBACK_NAME in names:
        raise ArtifactError("matcher names collide (S-22)")
    return lines, meta


def expected_snippet(lines: list[Line], meta: dict[str, str]) -> str:
    """§9.14.3 snippet shape, rendered here from the list (independent of the generator)."""
    out = [f"# {k} {meta[k]}" for k in HEADER_KEYS[:3]] + [f"# _format {SNIPPET_FORMAT}", f"({SNIPPET_NAME}) {{"]
    for line in lines:
        out += [f"\t@{line.matcher_name} {{", f"\t\tpath_regexp {line.regex}", f"\t\tmethod {' '.join(line.methods)}", "\t}",
                f"\thandle @{line.matcher_name} {{", f"\t\timport {UPSTREAM_SNIPPET}", "\t}"]
    out += [f"\t@{FALLBACK_NAME} {{", f"\t\tpath_regexp {FALLBACK_REGEX}", "\t}", f"\thandle @{FALLBACK_NAME} {{", "\t\trespond 404", "\t}", "}"]
    return "\n".join(out) + "\n"


def load_snippet(path: Path, lines: list[Line], meta: dict[str, str]) -> dict[str, str]:
    text = _read_ascii(path, "snippet")
    smeta = _parse_header(text.split("\n")[:4], SNIPPET_FORMAT, "snippet")
    for key in ("_yaml_sha256", "_phase_max"):
        if smeta[key] != meta[key]:
            raise ArtifactError(f"snippet {key} {smeta[key]} != list {meta[key]}")
    want = expected_snippet(lines, meta)
    if text != want:
        got_l, want_l = text.split("\n"), want.split("\n")
        n = next((i for i, (a, b) in enumerate(zip(got_l, want_l)) if a != b), min(len(got_l), len(want_l)))
        raise ArtifactError(f"snippet differs from the list-derived §9.14.3 shape at line {n + 1} "
                            f"(got {got_l[n][:80] if n < len(got_l) else '<eof>'!r}, want {want_l[n][:80] if n < len(want_l) else '<eof>'!r})")
    return smeta


def load_artifacts(paths: Path, snippet: Path | None) -> tuple[list[Line], dict[str, str]]:
    lines, meta = load_list(paths)
    load_snippet(snippet or paths.parent / SNIPPET_FILE, lines, meta)
    return lines, meta


def cmd_check_artifacts(args: argparse.Namespace) -> int:
    try:
        lines, meta = load_artifacts(args.paths, args.snippet)
    except ArtifactError as exc:
        print(f"CADDY_ARTIFACTS_FAILED {exc}")
        return 1
    for key, want in (("_phase_max", args.expect_phase_max), ("_yaml_sha256", args.expect_yaml_sha256)):
        if want and meta[key] != want:
            print(f"CADDY_ARTIFACTS_FAILED {key} {meta[key]} != expected {want}")
            return 1
    print(f"CADDY_ARTIFACTS_OK lines={len(lines)} list_format={LIST_FORMAT} snippet_format={SNIPPET_FORMAT} "
          f"yaml_sha256={meta['_yaml_sha256']} phase_max={meta['_phase_max']} fallback_verbatim=yes")
    return 0


# ---------------------------------------------------------------- RE2 semantics
_RE2_CACHE: dict[str, re.Pattern] = {}


def _re2_compile(pattern: str) -> re.Pattern:
    """Python pattern with RE2 (Go regexp, non-multiline) semantics for this subset: an unescaped '$'
    outside a character class matches only at the very end of the text (Python: also before a
    final newline), so it becomes ``\\Z``. RE2-only syntax fails to compile -> Uncomparable."""
    if pattern in _RE2_CACHE:
        return _RE2_CACHE[pattern]
    out, i, in_class = [], 0, False
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            out.append(pattern[i:i + 2])
            i += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
        elif c == "[":
            in_class = True
            if pattern[i + 1:i + 2] == "^":
                out.append("[^")
                i += 2
                if pattern[i:i + 1] == "]":
                    out.append("]")
                    i += 1
                continue
            if pattern[i + 1:i + 2] == "]":
                out.append("[]")
                i += 2
                continue
        elif c == "$":
            out.append(r"\Z")
            i += 1
            continue
        out.append(c)
        i += 1
    try:
        compiled = re.compile("".join(out))
    except re.error as exc:
        raise Uncomparable(f"path_regexp not evaluable here ({exc.msg})") from None
    _RE2_CACHE[pattern] = compiled
    return compiled


def re2_search(pattern: str, text: str) -> bool:
    return _re2_compile(pattern).search(text) is not None


# ---------------------------------------------------------------- emulator
@dataclass
class Request:
    method: str
    path: str
    host: str
    scheme: str = "https"


@dataclass
class Step:
    handler: dict
    route_trail: tuple[int, ...]
    route_match: list | None


@dataclass
class Outcome:
    chain: list[Step] = field(default_factory=list)
    responded: bool = False
    final_path: str = ""


def _caddy_path_match(pattern: str, path: str) -> bool:
    p = pattern.lower()
    s = path.lower()
    if p == "*":
        return True
    stars = p.count("*")
    if stars == 0:
        return s == p
    if stars == 2 and p.startswith("*") and p.endswith("*"):
        return p[1:-1] in s
    if stars == 1 and p.endswith("*"):
        return s.startswith(p[:-1])
    if stars == 1 and p.startswith("*"):
        return s.endswith(p[1:])
    rx = "^" + "".join("[^/]*" if c == "*" else re.escape(c) for c in p) + r"\Z"
    return re.match(rx, s) is not None


def _host_match(hosts: list, host: str) -> bool:
    host = host.lower()
    for h in hosts:
        h = str(h).lower()
        if h == host or (h.startswith("*.") and host.endswith(h[1:])):
            return True
    return False


def _match_one_set(mset: dict, req: Request) -> bool:
    for kind, value in mset.items():
        if kind not in EVALUABLE:
            raise Uncomparable(f"matcher '{kind}'")
        if kind == "host":
            if not _host_match(value, req.host):
                return False
        elif kind == "path":
            if not any(_caddy_path_match(p, req.path) for p in value):
                return False
        elif kind == "path_regexp":
            if not re2_search(value.get("pattern", ""), req.path):
                return False
        elif kind == "method":
            if req.method.upper() not in [m.upper() for m in value]:
                return False
        elif kind == "protocol":
            if value.lower() != req.scheme:
                return False
        elif kind == "not":
            if _match_sets(value, req):
                return False
    return True


def _match_sets(msets: list | None, req: Request) -> bool:
    if not msets:
        return True
    return any(_match_one_set(m, req) for m in msets)


def _apply_rewrite(handler: dict, req: Request) -> None:
    keys = set(handler) - {"handler"}
    if not keys <= {"strip_path_prefix", "strip_path_suffix"}:
        raise Uncomparable(f"rewrite fields {sorted(keys)}")
    prefix = handler.get("strip_path_prefix")
    if prefix and req.path.lower().startswith(prefix.lower()):
        req.path = req.path[len(prefix):] or "/"
    suffix = handler.get("strip_path_suffix")
    if suffix and req.path.endswith(suffix):
        req.path = req.path[: -len(suffix)]


def _eval_routes(routes: list, req: Request, trail: tuple[int, ...], out: Outcome, matches: tuple = ()) -> bool:
    done_groups: set[str] = set()
    for index, route in enumerate(routes):
        group = route.get("group")
        if group and group in done_groups:
            continue
        if not _match_sets(route.get("match"), req):
            continue
        if group:
            done_groups.add(group)
        here = trail + (index,)
        here_matches = matches + tuple(route.get("match") or ())
        for handler in route.get("handle", []):
            kind = handler.get("handler")
            if kind == "subroute":
                if _eval_routes(handler.get("routes", []), req, here, out, here_matches):
                    return True
                continue
            if kind == "rewrite":
                _apply_rewrite(handler, req)
            out.chain.append(Step(handler, here, list(here_matches)))
            if kind in RESPONDERS:
                out.responded = True
                out.final_path = req.path
                return True
        if route.get("terminal"):
            return False
    return False


def select_servers(config: dict, listen_port: str) -> list[dict]:
    servers = config.get("apps", {}).get("http", {}).get("servers", {})
    return [s for s in servers.values() if any(str(a).endswith(":" + listen_port) for a in s.get("listen", []))]


def _one_server(config: dict, listen_port: str) -> dict:
    servers = select_servers(config, listen_port)
    if not servers:
        raise Uncomparable(f"no server listens on :{listen_port}")
    if len(servers) > 1:
        raise Uncomparable(f"{len(servers)} servers listen on :{listen_port}")
    return servers[0]


def emulate(config: dict, req: Request, listen_port: str) -> Outcome:
    outcome = Outcome()
    _eval_routes(_one_server(config, listen_port).get("routes", []), req, (), outcome)
    if not outcome.responded:
        outcome.final_path = req.path
    return outcome


# ---------------------------------------------------------------- structure helpers
def _dials(handler: dict) -> list[str]:
    return [str(u.get("dial", "")) for u in handler.get("upstreams", [])]


def _header_ops(handler: dict) -> dict:
    if handler.get("handler") == "headers":
        return handler.get("request", {}) or {}
    if handler.get("handler") == "reverse_proxy":
        return (handler.get("headers") or {}).get("request", {}) or {}
    return {}


def _touched_names(ops: dict) -> set[str]:
    names: set[str] = set()
    for key in ("set", "add"):
        names |= {n.lower() for n in (ops.get(key) or {})}
    names |= {n.lower() for n in (ops.get("delete") or [])}
    names |= {n.lower() for n in (ops.get("replace") or {})}
    return names


def _touches(names: set[str], target: str) -> bool:
    target = target.lower()
    for name in names:
        if name == target:
            return True
        if name.endswith("*") and target.startswith(name[:-1]):
            return True
        if name.startswith("*") and target.endswith(name[1:]):
            return True
    return False


def _redact_path(value: str) -> str:
    """Paths and patterns in printed summaries go through o0_tool.redact_path (a path may carry a
    probe or webhook token); without o0_tool next to this file only the length is printed."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from o0_tool import redact_path
    except ImportError:
        return f"<path len={len(value)}>"
    return redact_path(value)


def _route_matcher_summary(match: list | None) -> str:
    if not match:
        return "<any>"
    parts = []
    for mset in match:
        items = []
        for kind, value in mset.items():
            if kind == "path_regexp":
                items.append(f"path_regexp={_redact_path(str(value.get('pattern')))!r}")
            elif kind == "path":
                items.append(f"path={','.join(_redact_path(str(v)) for v in value)}")
            elif kind in ("host", "method"):
                items.append(f"{kind}={','.join(map(str, value))}")
            else:
                items.append(kind)
        parts.append("&".join(items))
    return " | ".join(parts)


def _walk(routes: list, trail: tuple[int, ...] = ()):
    for index, route in enumerate(routes):
        here = trail + (index,)
        yield here, route
        for handler in route.get("handle", []):
            if handler.get("handler") == "subroute":
                yield from _walk(handler.get("routes", []), here)


def _walk_lists(routes: list, ancestry: tuple = ()):
    """Yield (ancestry, routes, index, route); ancestry = ((list, index, handler_index), ...) down to ``routes``."""
    for index, route in enumerate(routes):
        yield ancestry, routes, index, route
        for hi, handler in enumerate(route.get("handle", []) or []):
            if handler.get("handler") == "subroute":
                yield from _walk_lists(handler.get("routes", []) or [], ancestry + ((routes, index, hi),))


def _trail(ancestry: tuple, index: int) -> str:
    return ".".join(str(a[1]) for a in ancestry) + ("." if ancestry else "") + str(index)


def _flat_handlers(route: dict) -> list[dict]:
    out = []
    for h in route.get("handle", []) or []:
        if h.get("handler") == "subroute":
            for r in h.get("routes", []) or []:
                out += _flat_handlers(r)
        else:
            out.append(h)
    return out


def _mset_may_hit(mset: dict, path: str, host: str) -> bool:
    """F-13 step 1: may this matcher set match the probe? Only path, path_regexp (RE2) and host can
    exclude; method/protocol/not/expression/any other or unknown matcher counts as a hit."""
    for kind, value in mset.items():
        if kind == "path":
            if not any(_caddy_path_match(p, path) for p in value):
                return False
        elif kind == "path_regexp":
            try:
                if not re2_search(value.get("pattern", ""), path):
                    return False
            except Uncomparable:
                pass
        elif kind == "host":
            if not _host_match(value, host):
                return False
    return True


def _may_hit(match: list | None, path: str, host: str) -> bool:
    if not match:
        return True
    return any(_mset_may_hit(m, path, host) for m in match)


def _handler_shadow_problem(h: dict) -> str | None:
    kind = h.get("handler")
    if kind not in SHADOW_WHITELIST:
        return f"handler {kind!r} is not in the F-13 whitelist ({','.join(SHADOW_WHITELIST)})"
    if kind == "headers" and "request" in h:
        return "handler 'headers' with 'request' (request_header: may rewrite Authorization or X-Watcher-*)"
    return None


def _shadow_problems(route: dict) -> list[str]:
    problems = []
    handlers = route.get("handle") or []
    if not handlers:
        problems.append("empty handle")
    problems += [p for p in (_handler_shadow_problem(h) for h in handlers) if p]
    if route.get("terminal"):
        problems.append("terminal")
    if "group" in route:
        problems.append("group (a handle/handle_path block: mutually exclusive with the snippet, shadows it)")
    return problems


class Report:
    def __init__(self) -> None:
        self.passes = 0
        self.failures: list[str] = []

    def ok(self, msg: str) -> None:
        self.passes += 1
        print(f"PASS {msg}")

    def fail(self, msg: str) -> None:
        self.failures.append(msg)
        print(f"FAIL {msg}")


def _shadow_check(rep: Report, levels: list[tuple], host: str, label: str) -> None:
    """F-13 two-step check on every route that runs before the anchor. levels = [(list, index, handler_index|None), ...]
    from the server's route list down to the list that holds the anchor (handler_index None on the last level)."""
    checked = 0
    for depth, (routes, index, hi) in enumerate(levels):
        for j in range(index):
            route = routes[j]
            hits = [p for p in SHADOW_PROBES if _may_hit(route.get("match"), p, host)]
            where = f"{label} level {depth} route {j} ({_route_matcher_summary(route.get('match'))})"
            if not hits:
                print(f"INFO shadow {where}: no probe hit")
                continue
            checked += 1
            problems = _shadow_problems(route)
            kinds = ",".join(str(h.get("handler")) for h in route.get("handle") or [])
            if problems:
                rep.fail(f"shadow {where} hits {hits[0]!r} [{kinds}]: " + "; ".join(problems))
            else:
                rep.ok(f"shadow {where} hits {hits[0]!r} but only whitelisted handlers [{kinds}]")
        if hi is not None:
            container = routes[index]
            for h in (container.get("handle") or [])[:hi]:
                checked += 1
                problem = _handler_shadow_problem(h)
                where = f"{label} level {depth} container route {index} handler before its subroute"
                if problem:
                    rep.fail(f"shadow {where}: {problem}")
                else:
                    rep.ok(f"shadow {where}: {h.get('handler')} is whitelisted")
    print(f"INFO shadow {label}: {checked} route(s)/handler(s) before the anchor may see the prefix")


@dataclass
class WgwRoute:
    kind: str            # "line" | "fallback"
    ancestry: tuple
    routes: list
    index: int
    route: dict
    mset: dict


def _find_wgw(config: dict, listen_port: str, rep: Report, line_regex: set[str]) -> list[WgwRoute]:
    found: list[WgwRoute] = []
    for ancestry, routes, index, route in _walk_lists(_one_server(config, listen_port).get("routes", [])):
        for mset in route.get("match") or []:
            pattern = (mset.get("path_regexp") or {}).get("pattern")
            if pattern is None:
                continue
            if pattern == FALLBACK_REGEX:
                found.append(WgwRoute("fallback", ancestry, routes, index, route, mset))
            elif pattern.startswith("^" + WATCHER_PREFIX):
                found.append(WgwRoute("line", ancestry, routes, index, route, mset))
            elif EXTERNAL_PREFIX in pattern.lower() or "watcher" in pattern.lower():
                rep.fail(f"route {_trail(ancestry, index)}: path_regexp {_redact_path(pattern)!r} touches the watcher prefix but is neither a "
                         "list line nor the verbatim fallback (old fallback, (?i), altered or extra route)")
    return found


def _check_wgw_structure(rep: Report, config: dict, lines: list[Line], listen_port: str, upstream: str) -> list[WgwRoute]:
    """(a) set equality with the list, (b) fallback verbatim/404/last, contiguous block in ONE list."""
    wgw = _find_wgw(config, listen_port, rep, {l.regex for l in lines})
    # (a) all path_regexp starting ^/m/v1/watcher/ in the whole config, not only this server
    seen: list[tuple[str, tuple[str, ...]]] = []
    for _trail_, route in (x for s in config.get("apps", {}).get("http", {}).get("servers", {}).values() for x in _walk(s.get("routes", []))):
        for mset in route.get("match") or []:
            pattern = (mset.get("path_regexp") or {}).get("pattern", "")
            if pattern.startswith("^" + WATCHER_PREFIX):
                seen.append((pattern, tuple(sorted(mset.get("method") or []))))
    want = [(l.regex, tuple(sorted(l.methods))) for l in lines]
    extra = sorted(set(seen) - set(want))
    missing = sorted(set(want) - set(seen))
    dupes = sorted({x for x in seen if seen.count(x) > 1})
    if not seen:
        rep.fail("no ^/m/v1/watcher/ path_regexp in the adapted config (0 rows compared: uncomparable)")
    for pattern, methods in extra:
        rep.fail(f"adapted route not in the list: {pattern} {' '.join(methods) or '<no method matcher>'}")
    for pattern, methods in missing:
        rep.fail(f"list line missing from the adapted config: {pattern} {' '.join(methods)}")
    for pattern, methods in dupes:
        rep.fail(f"list line present more than once: {pattern} {' '.join(methods)}")
    if seen and not (extra or missing or dupes):
        rep.ok(f"adapted (pattern, methods) == list (regex, methods): symmetric difference empty, rows={len(seen)}")
    # every wgw matcher set is exactly {path_regexp[, method]} and its route has that one set only
    for w in wgw:
        keys = set(w.mset)
        want_keys = {"path_regexp"} if w.kind == "fallback" else {"path_regexp", "method"}
        if keys != want_keys or len(w.route.get("match") or []) != 1:
            rep.fail(f"route {_trail(w.ancestry, w.index)} ({w.kind}): matcher must be exactly {sorted(want_keys)} and the only "
                     f"matcher set of its route (got {sorted(keys)}, {len(w.route.get('match') or [])} set(s))")
    # (b) the fallback
    fallbacks = [w for w in wgw if w.kind == "fallback"]
    if len(fallbacks) != 1:
        rep.fail(f"fallback {FALLBACK_REGEX!r} present {len(fallbacks)} time(s), want exactly 1 (F-10)")
    else:
        fb = fallbacks[0]
        handlers = _flat_handlers(fb.route)
        if [(h.get("handler"), str(h.get("status_code"))) for h in handlers] != [("static_response", "404")] or \
                any(k not in ("handler", "status_code") for h in handlers for k in h):
            rep.fail(f"fallback route answers {[h.get('handler') for h in handlers]} - want exactly 'respond 404' (empty body)")
        else:
            rep.ok("fallback present once, verbatim, no method matcher, answers 404 only")
    # contiguous block, one list, fallback last
    lists = {id(w.routes) for w in wgw}
    if len(lists) > 1:
        rep.fail(f"wgw routes are split over {len(lists)} route lists (snippet imported more than once or partly nested)")
    elif wgw:
        idx = sorted({w.index for w in wgw})
        if idx != list(range(idx[0], idx[-1] + 1)):
            rep.fail(f"wgw routes are not contiguous in their list (indices {idx[0]}..{idx[-1]}, {len(idx)} routes)")
        elif fallbacks and fallbacks[0].index != idx[-1]:
            rep.fail(f"fallback at index {fallbacks[0].index} is not after the per-path routes (last wgw index {idx[-1]})")
        elif fallbacks:
            rep.ok(f"wgw block contiguous in one list ({len(idx)} routes), fallback last")
    return wgw


def _forwarder_check(rep: Report, routes: list, probes: list[str], host: str, upstream: str, skip: set[int], trail: tuple = ()) -> None:
    for index, route in enumerate(routes):
        if id(route) in skip:
            continue
        hits = [p for p in probes if _may_hit(route.get("match"), p, host)]
        if not hits:
            continue
        for h in route.get("handle", []) or []:
            if h.get("handler") == "subroute":
                _forwarder_check(rep, h.get("routes", []) or [], hits, host, upstream, skip, trail + (index,))
            elif h.get("handler") == "reverse_proxy":
                dials = _dials(h)
                if upstream in dials or any(d.rsplit(":", 1)[-1] in WATCHER_PORTS for d in dials):
                    rep.fail(f"route {'.'.join(map(str, trail + (index,)))} ({_route_matcher_summary(route.get('match'))}) may forward "
                             f"{hits[0]!r} to {','.join(dials)}: only the snippet may route the /m/v1/watcher prefix (§9.14.3, F-10)")


def _site_levels(config: dict, host: str, listen_port: str) -> tuple[list[tuple], list]:
    """Levels down to the app site's subroute list (before deploy there is no wgw anchor yet)."""
    top = _one_server(config, listen_port).get("routes", [])
    for i, route in enumerate(top):
        if not any(_host_match(m.get("host", []), host) for m in route.get("match") or [] if "host" in m):
            continue
        for hi, h in enumerate(route.get("handle", []) or []):
            if h.get("handler") == "subroute":
                return [(top, i, hi)], h.get("routes", []) or []
    raise Uncomparable(f"no route with host {host} and a subroute on :{listen_port}")


def _check_gateway_outcome(rep: Report, label: str, out: Outcome, line: Line, upstream: str) -> None:
    if not out.responded or out.chain[-1].handler.get("handler") != "reverse_proxy":
        rep.fail(f"{label}: no reverse_proxy responder")
        return
    last = out.chain[-1]
    if _dials(last.handler) != [upstream]:
        rep.fail(f"{label}: upstream {_dials(last.handler)} != [{upstream}]")
        return
    strips = [s for s in out.chain if s.handler.get("handler") == "rewrite"]
    if len(strips) != 1 or strips[0].handler.get("strip_path_prefix") != MOBILE_PREFIX:
        rep.fail(f"{label}: expected exactly one strip_path_prefix {MOBILE_PREFIX}, got {len(strips)} rewrite(s)")
        return
    if not out.final_path.startswith(APP_OUTER_PREFIX + "/"):
        rep.fail(f"{label}: upstream path {out.final_path!r} is not {APP_OUTER_PREFIX}/...")
        return
    for step in out.chain:
        kind = step.handler.get("handler")
        if kind == "authentication":
            rep.fail(f"{label}: mobile path passes through authentication (basic auth) handler")
            return
        names = _touched_names(_header_ops(step.handler))
        if _touches(names, "authorization") or any(_touches(names, h) for h in ("x-watcher-actor", "x-watcher-token-fingerprint", "x-watcher-proxy-auth")):
            rep.fail(f"{label}: {kind} rewrites Authorization or X-Watcher-* on the mobile path")
            return
    good = any("path_regexp" in m and m["path_regexp"].get("pattern") == line.regex and set(m.get("method", [])) == set(line.methods)
               for m in last.route_match or [])
    if not good:
        rep.fail(f"{label}: matched route is not the anchored line matcher ({_route_matcher_summary(last.route_match)})")
        return
    rep.ok(f"{label!r} -> {upstream}{out.final_path!r}")


def _reaches_gateway(out: Outcome, upstream: str) -> bool:
    return (
        out.responded
        and out.chain[-1].handler.get("handler") == "reverse_proxy"
        and upstream in _dials(out.chain[-1].handler)
        and out.final_path.lower().startswith(APP_OUTER_PREFIX)
    )


def _is_fallback_404(out: Outcome) -> bool:
    return (out.responded and out.chain[-1].handler.get("handler") == "static_response"
            and str(out.chain[-1].handler.get("status_code")) == "404"
            and any((m.get("path_regexp") or {}).get("pattern") == FALLBACK_REGEX for m in out.chain[-1].route_match or []))


def _check_browser_outcome(rep: Report, label: str, out: Outcome) -> bool:
    if not out.responded or out.chain[-1].handler.get("handler") != "reverse_proxy":
        return False
    dials = _dials(out.chain[-1].handler)
    if not any(d.rsplit(":", 1)[-1] in WATCHER_PORTS for d in dials):
        return False
    auth_seen = cleared_actor = cleared_fp = cleared_auth = False
    for step in out.chain[:-1]:
        kind = step.handler.get("handler")
        if kind == "authentication":
            auth_seen = True
        if kind == "headers":
            deleted = {n.lower() for n in (_header_ops(step.handler).get("delete") or [])}
            cleared_actor |= _touches(deleted, "x-watcher-actor")
            cleared_fp |= _touches(deleted, "x-watcher-token-fingerprint")
            cleared_auth |= _touches(deleted, "authorization")
    proxy_ops = _header_ops(out.chain[-1].handler)
    rp_deleted = {n.lower() for n in (proxy_ops.get("delete") or [])}
    cleared_actor |= _touches(rp_deleted, "x-watcher-actor")
    cleared_fp |= _touches(rp_deleted, "x-watcher-token-fingerprint")
    set_ops = {k.lower(): v for k, v in (proxy_ops.get("set") or {}).items()}
    problems = []
    if not auth_seen:
        problems.append("no basic auth before the watcher upstream (bypass)")
    if not (cleared_actor and cleared_fp):
        problems.append("X-Watcher-Actor / X-Watcher-Token-Fingerprint not cleared")
    if not (cleared_auth or "authorization" in rp_deleted):
        problems.append("browser Authorization (basic credentials) not cleared before the watcher")
    if _touches(rp_deleted, "x-watcher-proxy-auth"):
        problems.append("reverse_proxy deletes X-Watcher-Proxy-Auth (may run after set and drop the injection)")
    if set_ops.get("x-watcher-proxy-auth") != [BROWSER_TOKEN_PLACEHOLDER]:
        value = set_ops.get("x-watcher-proxy-auth")
        shown = "absent" if value is None else ("placeholder " + value[0] if value and re.fullmatch(r"\{[A-Za-z0-9_.]+\}", value[0] or "") else f"<literal len={len(value[0]) if value else 0}>")
        problems.append(f"X-Watcher-Proxy-Auth set is {shown}, expected {BROWSER_TOKEN_PLACEHOLDER}")
    if _touches(_touched_names(proxy_ops), "authorization") and "authorization" not in rp_deleted:
        problems.append("reverse_proxy sets Authorization towards the watcher")
    if problems:
        rep.fail(f"{label}: " + "; ".join(problems))
    else:
        rep.ok(f"{label} -> watcher via basic auth, cleared X-Watcher-* and Authorization, injected placeholder")
    return True


def _parse_sample(text: str) -> tuple[str, str]:
    method, _, path = text.partition(" ")
    if not path.startswith("/"):
        raise SystemExit(f"bad sample {text!r}; use 'METHOD /path'")
    return method.upper(), path


def line_negatives(lines: list[Line]) -> tuple[list[tuple[str, str]], list[tuple[str, str, Line]]]:
    """§9.14.4 item 1 per line: (negatives that must NOT match / reach the gateway, positives that MUST)."""
    neg: list[tuple[str, str]] = [("GET", WATCHER_PREFIX + "nope")]
    pos: list[tuple[str, str, Line]] = []
    for line in lines:
        s, m0 = line.sample(), line.methods[0]
        for m in HTTP_METHODS:
            if m not in line.methods:
                neg.append((m, s))
        neg += [(m0, s + "/"), (m0, s.upper()), (m0, s.replace("/watcher/", "/Watcher/", 1))]
        for k in range(line.param_count()):
            neg += [(m0, line.sample("", only=k)), (m0, line.sample("x/y", only=k))]
        if not line.has_param or not line.last_is_param:
            neg.append((m0, s + "/x"))
        if line.last_is_param:
            pos += [(m0, s + "\n", line), (m0, s + "#x", line)]
        else:
            neg += [(m0, s + "\n"), (m0, s + "#x")]
    return neg, pos


def run_verify(config: dict, lines: list[Line], *, host: str, listen_port: str, upstream: str,
               mobile_samples, browser_samples, before_deploy: bool = False) -> Report:
    rep = Report()

    def emu(method: str, path: str) -> Outcome | None:
        try:
            return emulate(config, Request(method, path, host), listen_port)
        except Uncomparable as exc:
            rep.fail(f"UNCOMPARABLE {method} {path!r}: {exc}")
            return None

    try:
        server = _one_server(config, listen_port)
    except Uncomparable as exc:
        rep.fail(f"UNCOMPARABLE {exc}")
        return rep
    skip: set[int] = set()
    if before_deploy:
        # site check before stage C: no snippet yet; report what would run before it (F-13 (2) record)
        wgw = _find_wgw(config, listen_port, rep, {l.regex for l in lines})
        if wgw:
            rep.fail(f"before-deploy: {len(wgw)} watcher gateway route(s) already present (stage C applied or partly applied?)")
        try:
            levels, site = _site_levels(config, host, listen_port)
            first_group = next((i for i, r in enumerate(site) if "group" in r), len(site))
            _shadow_check(rep, levels + [(site, first_group, None)], host, "prospective (routes before the first handle block)")
        except Uncomparable as exc:
            rep.fail(f"UNCOMPARABLE before-deploy site list: {exc}")
    else:
        wgw = _check_wgw_structure(rep, config, lines, listen_port, upstream)
        skip = {id(w.route) for w in wgw}
        if wgw and len({id(w.routes) for w in wgw}) == 1:
            first = min(wgw, key=lambda w: w.index)
            try:
                _levels, site_list = _site_levels(config, host, listen_port)
                if site_list is not first.routes:
                    rep.fail(f"wgw routes are not in the top-level route list of the {host} site (import nested in route/handle/handle_path: F-12)")
                else:
                    rep.ok(f"wgw routes sit in the top-level route list of the {host} site")
            except Uncomparable as exc:
                rep.fail(f"UNCOMPARABLE site list: {exc}")
            _shadow_check(rep, list(first.ancestry) + [(first.routes, first.index, None)], host, "snippet")
        for line in lines:
            for method in line.methods:
                out = emu(method, line.sample())
                if out is not None:
                    _check_gateway_outcome(rep, f"{method} {line.sample()}", out, line, upstream)
    _forwarder_check(rep, server.get("routes", []), list(PREFIX_PROBES), host, upstream, skip)
    neg, pos = line_negatives(lines)
    neg += [("GET", p) for p in FALLBACK_NO_MATCH]
    for method, path in neg:
        out = emu(method, path)
        if out is None:
            continue
        if _reaches_gateway(out, upstream):
            rep.fail(f"negative {method} {path!r} reaches the gateway (over-broad matcher or a forwarder of the prefix)")
        else:
            rep.ok(f"negative {method} {path!r} does not reach the gateway")
    if not before_deploy:
        for method, path, line in pos:
            out = emu(method, path)
            if out is not None:
                _check_gateway_outcome(rep, f"param-last {method} {path}", out, line, upstream)
        for path in FALLBACK_MATCH:
            out = emu("GET", path)
            if out is None:
                continue
            if _is_fallback_404(out):
                rep.ok(f"fallback GET {path!r} -> 404 by {FALLBACK_NAME}")
            else:
                last = out.chain[-1].handler.get("handler") if out.chain else "none"
                rep.fail(f"fallback GET {path!r} is not answered by the fallback 404 (responder={last})")
        for path in FALLBACK_NO_MATCH:
            out = emu("GET", path)
            if out is not None and _is_fallback_404(out):
                rep.fail(f"GET {path!r} answered by the fallback, but RE2 says it must not match (emulator or pattern drift)")
    for sample in mobile_samples:
        method, path = _parse_sample(sample)
        out = emu(method, path)
        if out is None:
            continue
        if not out.responded or upstream not in _dials(out.chain[-1].handler):
            rep.fail(f"mobile {sample}: does not reach {upstream}")
            continue
        strips = [s for s in out.chain if s.handler.get("handler") == "rewrite"]
        touched = any(_touches(_touched_names(_header_ops(s.handler)), "authorization") for s in out.chain)
        if len(strips) != 1 or touched:
            rep.fail(f"mobile {sample}: strips={len(strips)} authorization_touched={touched}")
        else:
            rep.ok(f"mobile {sample} -> {upstream}{out.final_path} (one strip, Authorization untouched)")
    watcher_routes = [(t, r) for t, r in _walk(server.get("routes", []))
                      if any(h.get("handler") == "reverse_proxy" and any(d.rsplit(":", 1)[-1] in WATCHER_PORTS for d in _dials(h))
                             for h in r.get("handle", []))]
    covered: set[tuple[int, ...]] = set()
    for sample in browser_samples:
        method, path = _parse_sample(sample)
        out = emu(method, path)
        if out is None:
            continue
        if before_deploy and out.responded and out.chain[-1].handler.get("handler") == "reverse_proxy" \
                and any(d.rsplit(":", 1)[-1] in WATCHER_PORTS for d in _dials(out.chain[-1].handler)):
            if any(s.handler.get("handler") == "authentication" for s in out.chain[:-1]):
                rep.ok(f"before-deploy browser {sample} -> watcher behind basic auth (injection not expected yet)")
            else:
                rep.fail(f"before-deploy browser {sample} reaches the watcher WITHOUT basic auth (bypass)")
            covered.add(out.chain[-1].route_trail)
            continue
        if _check_browser_outcome(rep, f"browser {sample}", out):
            covered.add(out.chain[-1].route_trail)
        else:
            last = out.chain[-1].handler.get("handler") if out.chain else "none"
            print(f"INFO browser {sample} does not reach the watcher (responder={last})")
    for trail, route in watcher_routes:
        if trail not in covered:
            rep.fail(f"route {trail} proxies to the watcher but no browser sample exercised it ({_route_matcher_summary(route.get('match'))}); add --browser-sample")
    return rep


def cmd_verify(args: argparse.Namespace) -> int:
    try:
        lines, meta = load_artifacts(args.paths, args.snippet)
    except ArtifactError as exc:
        print(f"CADDY_WATCHER_ROUTES_FAILED artifacts: {exc}")
        return 1
    if args.expect_phase_max and meta["_phase_max"] != args.expect_phase_max:
        print(f"FAIL list phase_max {meta['_phase_max']} != {args.expect_phase_max}")
        return 1
    config = json.loads(args.adapted.read_text(encoding="utf-8"))
    rep = run_verify(
        config, lines, host=args.host, listen_port=args.listen_port, upstream=args.upstream,
        mobile_samples=args.mobile_sample or DEFAULT_MOBILE_SAMPLES,
        browser_samples=args.browser_sample or DEFAULT_BROWSER_SAMPLES,
        before_deploy=args.before_deploy,
    )
    if rep.failures or rep.passes == 0:
        print(f"CADDY_WATCHER_ROUTES_FAILED failures={len(rep.failures)} passes={rep.passes}")
        return 1
    mode = "before_deploy" if args.before_deploy else "snippet"
    print(f"CADDY_WATCHER_ROUTES_OK mode={mode} lines={len(lines)} passes={rep.passes} yaml_sha256={meta['_yaml_sha256']} phase_max={meta['_phase_max']}")
    return 0


# ---------------------------------------------------------------- Caddyfile text checks (F-12, F-13 (2))
def _caddyfile_tokens(text: str) -> list[tuple[int, int, list[str]]]:
    """(line number, brace depth at line start, tokens) for every non-comment line."""
    out, depth = [], 0
    for n, raw in enumerate(text.splitlines(), 1):
        body = raw.strip()
        if not body or body.startswith("#"):
            continue
        toks = re.findall(r'"(?:\\.|[^"\\])*"|`[^`]*`|\S+', body)
        out.append((n, depth, toks))
        depth += sum(1 for t in toks if t == "{") - sum(1 for t in toks if t == "}")
        if depth < 0:
            raise ArtifactError(f"Caddyfile line {n}: unbalanced '}}'")
    return out


DIRECTIVE_WORD = re.compile(r"[a-z]{1,24}(?:_[a-z]{1,24}){0,3}")   # no digits: a token-like word never passes


def _record_args(args: list[str], redact) -> str:
    """Only what the F-13 (2) record needs: the matcher (named, path or '*'), then a count of the rest."""
    shown, rest = [], [a for a in args if a != "{"]
    if rest and (rest[0].startswith(("@", "/", "*")) and not rest[0].startswith("//")):
        shown.append(redact(rest[0]) if rest[0].startswith("/") else (rest[0] if re.fullmatch(r"@[A-Za-z0-9_-]{1,64}|\*", rest[0]) else "<matcher>"))
        rest = rest[1:]
    if rest:
        shown.append(f"<{len(rest)} more arg(s) hidden>")
    return " ".join(shown)


def caddyfile_check(text: str, redact) -> tuple[list[str], list[str]]:
    """Return (problems, record lines). redact: o0_tool.redact_line, applied to every printed path."""
    problems, record = [], []
    toks = _caddyfile_tokens(text)
    snippet_imports = [(n, d) for n, d, t in toks if t[0] == "import" and len(t) == 2 and os.path.basename(t[1]) == SNIPPET_FILE]
    if len(snippet_imports) != 1:
        problems.append(f"the snippet file {SNIPPET_FILE} must be imported exactly once (found {len(snippet_imports)})")
    elif snippet_imports[0][1] != 0:
        problems.append(f"line {snippet_imports[0][0]}: import {SNIPPET_FILE} must be at the top level (global position), not inside a block")
    else:
        n = snippet_imports[0][0]
        target = next(t[1] for ln, _d, t in toks if ln == n)
        if target != SNIPPET_FILE:
            record.append(f"NOTE line {n}: snippet imported by path {redact(target)} (the runbook uses the relative '{SNIPPET_FILE}', resolved next to the Caddyfile)")
    # site blocks: depth-0 lines that open a block and are not the global block, a snippet or a named route
    sites: list[tuple[int, str]] = []
    i = 0
    while i < len(toks):
        n, d, t = toks[i]
        if d == 0 and t[-1] == "{" and not (len(t) == 1 and i == 0) and not t[0].startswith("(") and t[0] != "&(":
            end = next((j for j in range(i + 1, len(toks)) if toks[j][1] == 0), len(toks))
            body = toks[i + 1:end]
            imports = [k for k, (_n, bd, bt) in enumerate(body) if bd == 1 and bt[:2] == ["import", SNIPPET_NAME]]
            if imports:
                sites.append((n, " ".join(x if re.fullmatch(r"[A-Za-z0-9.*:-]{1,253}|\{\$[A-Z0-9_]+\}", x.rstrip(",")) else f"<literal len={len(x)}>" for x in t[:-1])))
                handles = [k for k, (_n, bd, bt) in enumerate(body) if bd == 1 and bt[0] in ("handle", "handle_path", "route")]
                if len(imports) != 1:
                    problems.append(f"site at line {n}: import {SNIPPET_NAME} appears {len(imports)} times (want 1)")
                if handles and handles[0] < imports[0]:
                    problems.append(f"site at line {n}: '{body[handles[0]][2][0]}' at line {body[handles[0]][0]} comes BEFORE "
                                    f"import {SNIPPET_NAME} (F-12: the import must precede every handle/handle_path/route)")
                for _n, bd, bt in body:
                    if bd == 1 and bt[0] in PRE_HANDLE_DIRECTIVES:
                        record.append(f"RECORD site line {n} pre-handle directive line {_n}: {bt[0]} {_record_args(bt[1:], redact)}".rstrip()
                                      + "  -> may it hit /m/v1/watcher (any case)? why no shadow? (fill in)")
            i = end
            continue
        i += 1
    nested = [(n, d) for n, d, t in toks if t[:2] == ["import", SNIPPET_NAME] and d != 1]
    for n, d in nested:
        problems.append(f"line {n}: import {SNIPPET_NAME} at depth {d}: only at the top level of the app site block "
                        "(never inside handle_path/route/handle: F-12)")
    if not sites:
        problems.append(f"no site block imports {SNIPPET_NAME} at its top level")
    glob = toks[0] if toks and toks[0][2] == ["{"] and toks[0][1] == 0 else None
    orders = 0
    if glob:
        for n, d, t in toks[1:]:
            if d == 0:
                break
            if d == 1 and t[0] == "order":
                orders += 1
                words = " ".join(x if DIRECTIVE_WORD.fullmatch(x) else f"<literal len={len(x)}>" for x in t[1:])
                record.append(f"RECORD global line {n}: order {words}  -> does it move a directive before handle? (fill in)")
    record.append(f"RECORD global order options: {orders}")
    record.append(f"sites importing {SNIPPET_NAME}: " + "; ".join(f"line {n} [{addr}]" for n, addr in sites))
    return problems, record


def cmd_caddyfile_check(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from o0_tool import redact_line  # same directory in the repo and in bundle/tools
    try:
        problems, record = caddyfile_check(args.caddyfile.read_text(encoding="utf-8"), redact_line)
    except (ArtifactError, OSError, UnicodeDecodeError) as exc:
        print(f"CADDYFILE_CHECK_FAILED {type(exc).__name__}: {exc}")
        return 1
    print("\n".join(record))
    for p in problems:
        print(f"FAIL {p}")
    if problems:
        print(f"CADDYFILE_CHECK_FAILED problems={len(problems)}")
        return 1
    print("CADDYFILE_CHECK_OK snippet_import=top-level site_import=before-every-handle (F-13 (2) record above: fill in by hand)")
    return 0


# ---------------------------------------------------------------- inventory
def _handler_summary(h: dict) -> str:
    kind = h.get("handler")
    if kind == "reverse_proxy":
        ops = _header_ops(h)
        shown = []
        for op in ("set", "add"):
            for name, values in (ops.get(op) or {}).items():
                vals = [v if re.fullmatch(r"\{[A-Za-z0-9_.]+\}", v or "") else f"<literal len={len(v or '')}>" for v in values]
                shown.append(f"{op}:{name}={'|'.join(vals)}")
        for name in ops.get("delete") or []:
            shown.append(f"delete:{name}")
        return f"reverse_proxy dials={','.join(_dials(h))} {' '.join(shown)}".rstrip()
    if kind == "rewrite":
        return "rewrite " + " ".join(f"{k}={_redact_path(str(v))}" for k, v in h.items() if k != "handler")
    if kind == "authentication":
        providers = h.get("providers", {})
        basic = providers.get("http_basic", {})
        return f"authentication providers={','.join(providers)} accounts={len(basic.get('accounts', []))}"
    if kind == "headers":
        req = h.get("request") or {}
        resp = h.get("response") or {}
        return "headers " + " ".join(
            [f"req.{op}:{','.join(req[op])}" for op in ("set", "add", "delete", "replace") if req.get(op)]
            + [f"resp.{op}:{','.join(resp[op])}" for op in ("set", "add", "delete") if resp.get(op)]
        )
    if kind == "static_response":
        return f"static_response status={h.get('status_code', 200)}"
    if kind == "subroute":
        return f"subroute routes={len(h.get('routes', []))}"
    return str(kind)


def cmd_inventory(args: argparse.Namespace) -> int:
    config = json.loads(args.adapted.read_text(encoding="utf-8"))
    servers = config.get("apps", {}).get("http", {}).get("servers", {})
    for name, server in servers.items():
        print(f"server {name} listen={','.join(server.get('listen', []))}")
        for trail, route in _walk(server.get("routes", [])):
            indent = "  " * len(trail)
            group = f" group={route['group']}" if route.get("group") else ""
            term = " terminal" if route.get("terminal") else ""
            print(f"{indent}route {'.'.join(map(str, trail))}{group}{term} match={_route_matcher_summary(route.get('match'))}")
            for h in route.get("handle", []):
                if h.get("handler") != "subroute":
                    print(f"{indent}  - {_handler_summary(h)}")
    return 0


# ---------------------------------------------------------------- local Caddy probe (non-production)
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def probe_caddyfile(text: str, site_address: str, site_port: int, rewrites: dict[str, str], http_port: int, https_port: int) -> str:
    """Copy of the production Caddyfile for the local probe: ONLY the site address, the upstreams and
    the global admin/auto_https/http_port/https_port options change (the diff is recorded)."""
    lines = text.splitlines()
    first = next((i for i, l in enumerate(lines) if l.strip() and not l.strip().startswith("#")), None)
    probe_opts = ["\tadmin off", "\tauto_https off", f"\thttp_port {http_port}", f"\thttps_port {https_port}"]
    if first is not None and lines[first].strip() == "{":
        end = next(i for i in range(first + 1, len(lines)) if lines[i].strip() == "}" and not lines[i].startswith(("\t", " ")))
        body = [l for l in lines[first + 1:end] if l.strip().split(" ")[0] not in ("admin", "auto_https", "http_port", "https_port")]
        if any(l.strip().split(" ")[0] == "admin" and l.rstrip().endswith("{") for l in lines[first + 1:end]):
            raise ArtifactError("global 'admin { ... }' block: edit it by hand in the copy (the probe only rewrites one-line options)")
        lines = lines[:first + 1] + probe_opts + body + lines[end:]
    else:
        lines = ["{"] + probe_opts + ["}"] + lines
    hits = 0
    for i, l in enumerate(lines):
        if l and not l[0].isspace() and l.rstrip().endswith("{") and not l.startswith(("(", "{", "#")):
            head = l.rstrip()[:-1].rstrip()
            addrs = [a.strip() for a in re.split(r"[\s,]+", head) if a.strip()]
            if site_address in addrs:
                hits += 1
                lines[i] = f"http://127.0.0.1:{site_port} {{"
    if hits != 1:
        raise ArtifactError(f"site address {site_address!r} found in {hits} site header line(s), want exactly 1")
    out = "\n".join(lines) + "\n"
    for old, new in rewrites.items():
        if old not in out:
            raise ArtifactError(f"upstream {old} not found in the copy")
        out = out.replace(old, new)
    return out


class _Stub:
    def __init__(self, label: str, hits: list):
        import http.server

        class Handler(http.server.BaseHTTPRequestHandler):
            def _r(self):
                hits.append((label, self.command, self.path, self.headers.get("Authorization"), "X-Watcher-Proxy-Auth" in self.headers))
                body = f"STUB {label}".encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
            do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = do_OPTIONS = _r

            def log_message(self, *_a):
                pass

        self.port = _free_port()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def _raw_request(port: int, method: str, target: str, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    extra = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
    s.sendall(f"{method} {target} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n{extra}Content-Length: 0\r\nConnection: close\r\n\r\n".encode("latin1"))
    data = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), body


def cmd_probe(args: argparse.Namespace) -> int:
    try:
        lines, meta = load_artifacts(args.paths, args.snippet)
    except ArtifactError as exc:
        print(f"CADDY_PROBE_FAILED artifacts: {exc}")
        return 1
    copy_path = args.caddyfile.resolve()
    staged = copy_path.parent / SNIPPET_FILE
    if not staged.is_file() or staged.read_bytes() != (args.snippet or args.paths.parent / SNIPPET_FILE).read_bytes():
        print(f"CADDY_PROBE_FAILED {staged} must be a byte-identical copy of the committed snippet (the import is relative)")
        return 1
    hits: list = []
    oq, wa = _Stub("oq", hits), _Stub("watcher", hits)
    port, hp, hsp = _free_port(), _free_port(), _free_port()
    original = copy_path.read_text(encoding="utf-8")
    try:
        text = probe_caddyfile(original, args.site_address, port, {args.upstream: f"127.0.0.1:{oq.port}",
                                                                   args.watcher_upstream: f"127.0.0.1:{wa.port}"}, hp, hsp)
    except ArtifactError as exc:
        print(f"CADDY_PROBE_FAILED {exc}")
        return 1
    probe_file = copy_path.with_name(copy_path.name + ".o0probe")
    probe_file.write_text(text, encoding="utf-8")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from o0_tool import redact_line
    diff = [redact_line(d) for d in difflib.unified_diff(original.splitlines(), text.splitlines(), "production-copy", "probe", n=0, lineterm="")]
    print("---- probe copy vs production copy (line diff, redacted)")
    print("\n".join(diff))
    env = dict(os.environ)
    # Caddy's autosave.json and data dir go to a throwaway directory, never ~/.config/caddy or /var/lib/caddy
    xdg = Path(tempfile.mkdtemp(prefix="o0-caddy-probe-xdg-"))
    env.update(XDG_CONFIG_HOME=str(xdg / "config"), XDG_DATA_HOME=str(xdg / "data"), HOME=str(xdg))
    for kv in args.adapt_env or []:
        k, _, v = kv.partition("=")
        env[k] = v
    version = subprocess.run([args.caddy, "version"], capture_output=True, text=True).stdout.strip().split(" ")[0]
    print(f"caddy version {version}")
    adapted = subprocess.run([args.caddy, "adapt", "--adapter", "caddyfile", "--config", str(probe_file)], env=env,
                             stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if adapted.returncode != 0:
        print(f"CADDY_PROBE_FAILED adapt rc={adapted.returncode} stderr_lines={len(adapted.stderr.splitlines())}")
        return 1
    rep = run_verify(json.loads(adapted.stdout), lines, host="127.0.0.1", listen_port=str(port), upstream=f"127.0.0.1:{oq.port}",
                     mobile_samples=DEFAULT_MOBILE_SAMPLES, browser_samples=DEFAULT_BROWSER_SAMPLES)
    fails = [f"verify: {f}" for f in rep.failures]
    n = 0
    proc = subprocess.Popen([args.caddy, "run", "--config", str(probe_file), "--adapter", "caddyfile"], env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(150):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)

        def expect(cond: bool, msg: str) -> None:
            nonlocal n
            n += 1
            if not cond:
                fails.append(msg)

        def forwarded(method, target, path, headers=None):
            before = len(hits)
            status, body = _raw_request(port, method, target, headers)
            ok = status == 200 and len(hits) == before + 1 and hits[-1][:3] == ("oq", method, path)
            expect(ok, f"forward {method} {target!r} -> {status} hits={[h[:3] for h in hits[before:]]} want oq {path!r}")
            return hits[-1] if ok else None

        def fallback404(method, target):
            before = len(hits)
            status, body = _raw_request(port, method, target)
            expect(status == 404 and body == b"" and len(hits) == before, f"fallback {method} {target!r} -> {status} body={len(body)}B hits={len(hits) - before}")

        def not_forwarded(method, target):
            before = len(hits)
            status, _body = _raw_request(port, method, target)
            expect(len(hits) == before, f"{method} {target!r} -> {status} reached a stub {[h[:3] for h in hits[before:]]}")

        for line in lines:
            ex = line.sample("x")
            for m in HTTP_METHODS:
                if m in line.methods:
                    forwarded(m, ex, ex[len(MOBILE_PREFIX):])
                else:
                    fallback404(m, ex)
            m0 = line.methods[0]
            fallback404(m0, ex.upper())
            fallback404(m0, ex + "/")
            for k in range(line.param_count()):
                fallback404(m0, line.sample("", only=k))
                fallback404(m0, line.sample("x/y", only=k))
            if line.last_is_param:
                forwarded(m0, ex + "%0A", ex[len(MOBILE_PREFIX):] + "%0A")
                forwarded(m0, ex + "#x", ex[len(MOBILE_PREFIX):] + "%23x")
            else:
                fallback404(m0, ex + "/x")
                fallback404(m0, ex + "%0A")
                fallback404(m0, ex + "#x")
        for target in ("/m/v1/watcher", "/m/v1/watcher/", "/M/V1/WATCHER/login/start", "/m/v1/watcher/login/start", "/m/v1/watcher%0A",
                       "/M/V1/WATCHER%0A", "/m/v1/watcher/status%0A", "/m/v1/watcher/status#x", "/m/v1/watcher/config", "/m/V1/watcher/status"):
            fallback404("GET", target)
        for target in ("/m/v1/watcherx", "/m/v1/other", "/m/v1/watcherx%0A", "/m/v1/watcher%0D", "/m/v1/watcher#x"):
            not_forwarded("GET", target)
        # F-13 (3): literal dot segments and // are CLEANED by uri strip_prefix and forwarded (never expected as 404)
        forwarded("GET", "/m/v1/watcher/x/../status", "/v1/watcher/status")
        forwarded("GET", "/m/v1/watcher/./status", "/v1/watcher/status")
        forwarded("GET", "/m/v1/watcher//status", "/v1/watcher/status")
        forwarded("GET", "/m/v1/watcher/trading/accounts/a/..", "/v1/watcher/trading/accounts")
        fallback404("GET", "/m/v1/watcher/x/../config")
        # encoded forms and '#' are forwarded raw (the gateway's '%' rule answers 404 there)
        forwarded("GET", "/m/v1/watcher/x/%2e%2e/status", "/v1/watcher/x/%2e%2e/status")
        forwarded("GET", "/m/v1/watcher/media/a#x", "/v1/watcher/media/a%23x")
        # mobile Authorization kept, nothing injected; browser paths behind basic auth
        fake = "Bearer o0-probe-not-a-token"
        hit = forwarded("GET", "/m/v1/watcher/status", "/v1/watcher/status", {"Authorization": fake})
        expect(bool(hit) and hit[3] == fake and not hit[4], "watcher gateway path: Authorization changed or X-Watcher-Proxy-Auth injected")
        before = len(hits)
        status, _b = _raw_request(port, "GET", "/m/v1/accounts", {"Authorization": fake})
        mob = hits[before:]
        expect(len(mob) == 1 and mob[0][:3] == ("oq", "GET", "/v1/accounts") and mob[0][3] == fake and not mob[0][4],
               f"/m/v1/accounts: want oq /v1/accounts with the caller's Authorization (got {[h[:3] for h in mob]})")
        for target in ("/watcher/", "/api/status", "/media/1700000000000-1.jpg"):
            before = len(hits)
            status, _b = _raw_request(port, "GET", target)
            expect(status == 401 and len(hits) == before, f"browser {target} without credentials -> {status}, stub hits {len(hits) - before}")
    finally:
        proc.terminate()
        proc.wait()
        oq.server.shutdown()
        wa.server.shutdown()
        if not args.keep:
            probe_file.unlink()
        for q in sorted(xdg.rglob("*"), reverse=True):
            q.unlink() if (q.is_symlink() or q.is_file()) else q.rmdir()
        xdg.rmdir()
    for f in fails[:40]:
        print(f"FAIL {f}")
    if fails:
        print(f"CADDY_PROBE_FAILED caddy={version} checks={n} verify_passes={rep.passes} failures={len(fails)}")
        return 1
    print(f"CADDY_PROBE_OK caddy={version} live_checks={n} verify_passes={rep.passes} lines={len(lines)} "
          f"yaml_sha256={meta['_yaml_sha256']} phase_max={meta['_phase_max']} dot_and_double_slash=cleaned_and_forwarded")
    return 0


# ---------------------------------------------------------------- selftest
def _fixture(lines: list[Line]) -> dict:
    """Shaped like real Caddy v2.10.2 ``caddy adapt`` output of tests/fixtures/caddy/Caddyfile.prodlike.in
    (import at the top of the site block; tests/caddy_real_test.sh checks the real thing)."""
    def proxy(dial, headers=None):
        h = {"handler": "reverse_proxy", "upstreams": [{"dial": dial}]}
        if headers:
            h["headers"] = {"request": headers}
        return h

    def strip_proxy():
        return {"handler": "subroute", "routes": [{"handle": [{"handler": "rewrite", "strip_path_prefix": "/m"}, proxy("127.0.0.1:8183")]}]}

    g = "group21"
    site: list = [{"handle": [{"handler": "headers", "response": {"set": {"X-Content-Type-Options": ["nosniff"]}}},
                              {"handler": "encode", "encodings": {"gzip": {}}, "prefer": ["gzip"]}]}]
    for line in lines:
        site.append({"group": g, "match": [{"method": list(line.methods), "path_regexp": {"name": line.matcher_name, "pattern": line.regex}}],
                     "handle": [strip_proxy()]})
    site.append({"group": g, "match": [{"path_regexp": {"name": FALLBACK_NAME, "pattern": FALLBACK_REGEX}}],
                 "handle": [{"handler": "subroute", "routes": [{"handle": [{"handler": "static_response", "status_code": 404}]}]}]})
    site.append({"group": g, "match": [{"path": ["/m/v1/accounts", "/m/v1/mirror/positions", "/m/v1/operator/orders", "/m/v1/operator/orders/*"]}],
                 "handle": [strip_proxy()]})
    site.append({"group": g, "match": [{"path": ["/v1/*"]}], "handle": [{"handler": "subroute", "routes": [{"handle": [
        proxy("127.0.0.1:8183", {"set": {"Authorization": ["Bearer {env.SYSTEM_OBSERVER_TOKEN}"]}})]}]}]})
    site.append({"group": g, "match": [{"path": ["/watcher", "/watcher/*", "/api/*", "/media/*", "/healthz"]}],
                 "handle": [{"handler": "subroute", "routes": [{"handle": [
                     {"handler": "rewrite", "strip_path_prefix": "/watcher"},
                     {"handler": "authentication", "providers": {"http_basic": {"accounts": [{"password": "HASH", "username": "u"}], "hash": {"algorithm": "bcrypt"}}}},
                     {"handler": "headers", "request": {"delete": ["X-Watcher-Actor"]}},
                     {"handler": "headers", "request": {"delete": ["X-Watcher-Token-Fingerprint"]}},
                     {"handler": "headers", "request": {"delete": ["X-Watcher-Proxy-Auth"]}},
                     {"handler": "headers", "request": {"delete": ["Authorization"]}},
                     proxy("127.0.0.1:9090", {"set": {"X-Watcher-Proxy-Auth": [BROWSER_TOKEN_PLACEHOLDER]}})]}]}]})
    site.append({"group": g, "handle": [{"handler": "subroute", "routes": [{"handle": [
        {"handler": "vars", "root": "/srv/trader-dashboard/dist"}, {"handler": "file_server"}]}]}]})
    return {"apps": {"http": {"servers": {
        "srv0": {"listen": [":443"], "routes": [{"match": [{"host": ["jp-bot.balen.wang"]}], "handle": [{"handler": "subroute", "routes": site}], "terminal": True}]},
        "srv1": {"listen": ["127.0.0.1:8080"], "routes": [{"handle": [proxy("127.0.0.1:8183")]}]},
    }}}}


def _site(cfg: dict) -> list:
    return cfg["apps"]["http"]["servers"]["srv0"]["routes"][0]["handle"][0]["routes"]


def cmd_selftest(args: argparse.Namespace) -> int:
    import contextlib
    import io

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from o0_tool import caddy_skeleton, redact_line  # the structure the site check exports from jp-24

    lines, meta = load_artifacts(args.paths, args.snippet)
    snippet_path = args.snippet or args.paths.parent / SNIPPET_FILE
    checks = 0

    # 1. RE2 pins: the emulator must agree with Go regexp on the contract's probes (§9.14.4 item 1)
    for p in FALLBACK_MATCH:
        assert re2_search(FALLBACK_REGEX, p), ("fallback must match", p)
    for p in FALLBACK_NO_MATCH:
        assert not re2_search(FALLBACK_REGEX, p), ("fallback must not match", p)
    assert not re2_search("^/m/v1/watcher/status$", "/m/v1/watcher/status\n"), "RE2 '$' is end of text only"
    assert re.search("^/m/v1/watcher/status$", "/m/v1/watcher/status\n"), "sanity: Python '$' differs (this is why the emulator translates)"
    assert re2_search("^/m/v1/watcher/media/[^/]+$", "/m/v1/watcher/media/a\n") and re2_search("^a[$]$", "a$")
    assert not re2_search("^/m/v1/watcher/status$", "/M/V1/WATCHER/status"), "path_regexp is case-sensitive"
    try:
        re2_search(r"^/\pL+$", "/a")
        raise AssertionError("RE2-only syntax must be uncomparable here")
    except Uncomparable:
        pass
    checks += 7

    # 2. artifact parser negatives (list v2 + snippet)
    base = Path(tempfile.mkdtemp(prefix="o0-caddy-selftest-"))
    try:
        good_list = args.paths.read_text(encoding="ascii")
        good_snip = snippet_path.read_text(encoding="ascii")

        def art(list_text: str, snip_text: str) -> str | None:
            (base / "l.txt").write_text(list_text, encoding="latin1")
            (base / SNIPPET_FILE).write_text(snip_text, encoding="latin1")
            try:
                load_artifacts(base / "l.txt", base / SNIPPET_FILE)
            except ArtifactError as exc:
                return str(exc)
            return None

        assert art(good_list, good_snip) is None
        row = next(l for l in good_list.splitlines() if "{" in l)
        first_row = next(l for l in good_list.splitlines() if not l.startswith("#"))
        list_bad = {
            "_format v1": good_list.replace(LIST_FORMAT, "watcher-gateway-caddy-paths.v1"),
            "_format missing": good_list.replace(f"# _format {LIST_FORMAT}\n", ""),
            "'*' row (v1 writing)": good_list.replace(row, row.split(" ")[0].replace(row.split(" ")[0].rsplit("/", 1)[1], "*") + " " + " ".join(row.split(" ")[1:])),
            "regex not derived from the template": good_list.replace(row, row.replace("[^/]+$", "[^/]*$")),
            "(?i) regex": good_list.replace(row, row.replace(" ^/", " (?i)^/")),
            "unsorted methods": good_list.replace(" GET HEAD", " HEAD GET"),
            "unsorted rows": good_list.replace(first_row + "\n", "") + first_row + "\n",
            "CRLF": good_list.replace("\n", "\r\n"),
            "no rows": "\n".join(good_list.splitlines()[:4]) + "\n",
            "upper-case segment": good_list.replace(first_row, first_row.replace("/dialogs", "/Dialogs", 1)),
        }
        for name, text in list_bad.items():
            assert art(text, good_snip) is not None, ("list accepted", name)
            checks += 1
        snip_bad = {
            "_format": good_snip.replace(SNIPPET_FORMAT, "watcher-gateway-caddy-snippet.v0"),
            "old fallback (?:/|$)": good_snip.replace(FALLBACK_REGEX, "^(?i:/m/v1/watcher)(?:/|$)"),
            "fallback removed": good_snip.replace(f"\t@{FALLBACK_NAME} {{\n\t\tpath_regexp {FALLBACK_REGEX}\n\t}}\n\thandle @{FALLBACK_NAME} {{\n\t\trespond 404\n\t}}\n", ""),
            "fallback answers 200": good_snip.replace("respond 404", "respond 200"),
            "(?i) on a line": good_snip.replace("path_regexp ^/m/v1/watcher/status$", "path_regexp (?i)^/m/v1/watcher/status$"),
            "extra method": good_snip.replace("path_regexp ^/m/v1/watcher/status$\n\t\tmethod GET", "path_regexp ^/m/v1/watcher/status$\n\t\tmethod GET PATCH"),
            "upstream address in the snippet": good_snip.replace("\t\timport watcher_gateway_upstream\n", "\t\treverse_proxy 127.0.0.1:8183\n", 1),
            "yaml sha differs": good_snip.replace(meta["_yaml_sha256"], "0" * 64),
        }
        for name, text in snip_bad.items():
            assert art(good_list, text) is not None, ("snippet accepted", name)
            checks += 1

        # 3. Caddyfile text checks (F-12 position, F-13 (2) record)
        cf_good = ("{\n\tadmin localhost:2019\n\torder rate_limit before basic_auth\n}\nimport caddy-watcher-gateway.caddy\n"
                   "(watcher_gateway_upstream) {\n\turi strip_prefix /m\n\treverse_proxy 127.0.0.1:8183\n}\n"
                   "jp-bot.balen.wang {\n\timport watcher_gateway_routes\n\tencode gzip\n\theader X-Frame-Options DENY\n"
                   "\thandle /v1/* {\n\t\treverse_proxy 127.0.0.1:8183\n\t}\n}\n")
        probs, record = caddyfile_check(cf_good, redact_line)
        assert not probs, probs
        assert any("order rate_limit before basic_auth" in r for r in record) and sum("pre-handle directive" in r for r in record) == 2, record
        leaky = cf_good.replace("\theader X-Frame-Options DENY\n", "\theader X-Api-Key SENTINELhdr12\n\trequest_header @m Authorization \"Bearer SENTINELbearer\"\n"
                                "\trewrite /hook/SENTINELpath0123456789/* /x\n").replace("\torder rate_limit before basic_auth\n", "\torder SENTINELord9x before handle\n")
        probs, record = caddyfile_check(leaky, redact_line)
        assert not probs and "SENTINEL" not in "\n".join(record), ("caddyfile-check printed a secret", record)
        assert any("request_header @m <2 more arg(s) hidden>" in r for r in record), record
        checks += 1
        for name, text in {
            "handle before the import": cf_good.replace("\timport watcher_gateway_routes\n", "").replace("\t}\n}\n", "\t}\n\timport watcher_gateway_routes\n}\n"),
            "import inside route {}": cf_good.replace("\timport watcher_gateway_routes\n", "\troute {\n\t\timport watcher_gateway_routes\n\t}\n"),
            "snippet file imported inside the site": cf_good.replace("import caddy-watcher-gateway.caddy\n", "").replace("\timport watcher_gateway_routes\n", "\timport caddy-watcher-gateway.caddy\n\timport watcher_gateway_routes\n"),
            "snippet file not imported": cf_good.replace("import caddy-watcher-gateway.caddy\n", ""),
            "site does not import the routes": cf_good.replace("\timport watcher_gateway_routes\n", ""),
            "imported twice": cf_good.replace("\timport watcher_gateway_routes\n", "\timport watcher_gateway_routes\n\timport watcher_gateway_routes\n"),
        }.items():
            assert caddyfile_check(text, redact_line)[0], ("caddyfile check accepted", name)
            checks += 1
    finally:
        for p in sorted(base.rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
        base.rmdir()

    # 4. adapted-config verification: good fixture, skeleton, broken variants (raw and skeleton)
    def verdict(cfg: dict, before_deploy: bool = False) -> tuple[bool, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rep = run_verify(cfg, lines, host="jp-bot.balen.wang", listen_port="443", upstream=DEFAULT_GATEWAY_UPSTREAM,
                             mobile_samples=DEFAULT_MOBILE_SAMPLES, browser_samples=DEFAULT_BROWSER_SAMPLES,
                             before_deploy=before_deploy)
        return (not rep.failures and rep.passes > 0), buf.getvalue()

    good = _fixture(lines)
    ok, text = verdict(good)
    assert ok, text
    passes = text.count("\nPASS ") + text.startswith("PASS ")
    ok_skel, text_skel = verdict(caddy_skeleton(good))
    assert ok_skel, "the redacted skeleton must keep everything the verifier needs:\n" + text_skel

    def mutated(fn):
        cfg = copy.deepcopy(good)
        fn(_site(cfg))
        return cfg

    def gw_index(site, needle):
        for i, r in enumerate(site):
            for m in r.get("match") or []:
                if needle in (m.get("path_regexp") or {}).get("pattern", ""):
                    return i
        raise AssertionError(needle)

    def set_pattern(site, needle, pattern):
        site[gw_index(site, needle)]["match"][0]["path_regexp"]["pattern"] = pattern

    def first_wgw(site):
        return gw_index(site, "^/m/v1/watcher/")

    def fb(site):
        return gw_index(site, "(?i:")

    def strip_proxy_route(match, group="group21"):
        r = {"match": match, "handle": [{"handler": "subroute", "routes": [{"handle": [
            {"handler": "rewrite", "strip_path_prefix": "/m"}, {"handler": "reverse_proxy", "upstreams": [{"dial": "127.0.0.1:8183"}]}]}]}]}
        if group:
            r["group"] = group
        return r

    def wrap_in_route(site):  # import wrapped in route { } -> nested list after the handle blocks
        i, j = first_wgw(site), fb(site)
        block = site[i:j + 1]
        del site[i:j + 1]
        for r in block:
            r["group"] = "group17"
        site.append({"handle": [{"handler": "subroute", "routes": block}]})   # real adapt: route {} sorts after every handle

    def wrap_in_route_no_catchall(site):  # same, but nothing hits before it: only the "site top level" rule catches it
        site.pop()
        wrap_in_route(site)

    browser = lambda s: s[-2]["handle"][0]["routes"][0]["handle"]  # noqa: E731
    variants = {
        # route set (a)
        "missing list line": lambda s: s.pop(gw_index(s, "trading/briefings")),
        "extra method on a line": lambda s: s[gw_index(s, "watcher/status")]["match"][0].update({"method": ["GET", "PATCH"]}),
        "unlisted /m/v1/watcher route": lambda s: s.insert(first_wgw(s), strip_proxy_route([{"path_regexp": {"name": "x", "pattern": "^/m/v1/watcher/price-alerts$"}}])),
        "regexp allows several segments": lambda s: set_pattern(s, "trading/risks/", "^/m/v1/watcher/trading/risks/.+$"),
        "regexp allows an empty segment": lambda s: set_pattern(s, "trading/channels/", "^/m/v1/watcher/trading/channels/[^/]*$"),
        "regexp made case-insensitive": lambda s: set_pattern(s, "trading/accounts/", "(?i)^/m/v1/watcher/trading/accounts/[^/]+$"),
        "regexp not anchored at the end": lambda s: set_pattern(s, "media/", "^/m/v1/watcher/media/[^/]+"),
        "prefix path matcher instead of anchored regexp": lambda s: s[gw_index(s, "trading/accounts/")]["match"][0].update(
            {"path": ["/m/v1/watcher/trading/accounts/*"]}) or s[gw_index(s, "trading/accounts/")]["match"][0].pop("path_regexp"),
        "line matcher widened by a second matcher set": lambda s: s[gw_index(s, "watcher/status")]["match"].append({"path": ["/m/*"]}),
        # fallback (b)
        "fallback missing": lambda s: s.pop(fb(s)),
        "old fallback (?:/|$)": lambda s: set_pattern(s, "(?i:", "^(?i:/m/v1/watcher)(?:/|$)"),
        "fallback proxies instead of 404": lambda s: s.__setitem__(fb(s), strip_proxy_route(s[fb(s)]["match"])),
        "fallback answers 200": lambda s: s[fb(s)]["handle"][0]["routes"][0]["handle"][0].update({"status_code": 200}),
        "fallback not last (before the per-path routes)": lambda s: s.insert(first_wgw(s), s.pop(fb(s))),
        "fallback with a method matcher": lambda s: s[fb(s)]["match"][0].update({"method": ["GET"]}),
        "fallback duplicated": lambda s: s.insert(fb(s) + 1, copy.deepcopy(s[fb(s)])),
        # F-12 / F-13 shadow (c)
        "handle /m/* before the import (F-12)": lambda s: s.insert(first_wgw(s), strip_proxy_route([{"path": ["/m/*"]}])),
        "top-level rewrite hits the prefix (F-13 a)": lambda s: s.insert(0, {"match": [{"path": ["/m/v1/watcher/dialogs"]}], "handle": [
            {"handler": "rewrite", "uri": "/m/v1/watcher/status"}]}),
        "top-level request_header (no matcher)": lambda s: s[0]["handle"].insert(0, {"handler": "headers", "request": {"set": {"Authorization": ["Bearer {env.SYSTEM_OBSERVER_TOKEN}"]}}}),
        "headers with request and response": lambda s: s[0]["handle"][0].update({"request": {"delete": ["X-Forwarded-For"]}}),
        "top-level basic_auth /m/*": lambda s: s.insert(0, {"match": [{"path": ["/m/*"]}], "handle": [
            {"handler": "authentication", "providers": {"http_basic": {"accounts": [{"password": "HASH", "username": "u"}]}}}]}),
        "top-level forward_auth (reverse_proxy) without matcher": lambda s: s.insert(0, {"handle": [
            {"handler": "reverse_proxy", "upstreams": [{"dial": "127.0.0.1:7000"}], "handle_response": [{"match": {"status_code": [2]}, "routes": []}]}]}),
        "whitelisted handler but terminal": lambda s: s[0].update({"terminal": True}),
        "empty handle before the snippet": lambda s: s.insert(0, {"handle": []}),
        "not-matcher (uncomparable = hit) with a redir": lambda s: s.insert(0, {"match": [{"not": [{"path": ["/static/*"]}]}], "handle": [
            {"handler": "static_response", "status_code": 308, "headers": {"Location": ["/x"]}}]}),
        "expression matcher before gateway": lambda s: s.insert(0, {"match": [{"expression": "{path}.startsWith('/m/')"}], "handle": [{"handler": "static_response", "status_code": 404}]}),
        "case-sensitive path_regexp ^/M/ hits the upper-case probe": lambda s: s.insert(0, {"match": [{"path_regexp": {"name": "u", "pattern": "^/M/"}}], "handle": [
            {"handler": "rewrite", "strip_path_prefix": "/M"}]}),
        "unknown plugin handler before the snippet": lambda s: s.insert(0, {"handle": [{"handler": "rate_limit"}]}),
        "import wrapped in route { } (after the handle blocks)": wrap_in_route,
        "import wrapped in route { } without a catch-all handle": wrap_in_route_no_catchall,
        "snippet inside handle_path /m* (extra strip, nested)": lambda s: s.insert(first_wgw(s), {"group": "group21", "match": [{"path": ["/m*"]}], "handle": [
            {"handler": "rewrite", "strip_path_prefix": "/m"}, {"handler": "subroute", "routes": [s.pop(first_wgw(s))]}]}),
        # forwarders of the prefix (d) and emulation (e)
        "/m/* forwarder after the fallback (watcherx reaches OQ)": lambda s: s.insert(fb(s) + 1, strip_proxy_route([{"path": ["/m/*"]}])),
        "double strip": lambda s: s[gw_index(s, "watcher/status")]["handle"][0]["routes"].insert(0, {"handle": [{"handler": "rewrite", "strip_path_prefix": "/m"}]}),
        "upstream snippet injects the panel Authorization": lambda s: s[gw_index(s, "watcher/status")]["handle"][0]["routes"][0]["handle"][1].update(
            {"headers": {"request": {"set": {"Authorization": ["Bearer {env.SYSTEM_OBSERVER_TOKEN}"]}}}}),
        "upstream snippet injects X-Watcher-Proxy-Auth": lambda s: s[gw_index(s, "dialogs")]["handle"][0]["routes"][0]["handle"][1].update(
            {"headers": {"request": {"set": {"X-Watcher-Proxy-Auth": [BROWSER_TOKEN_PLACEHOLDER]}}}}),
        "wrong upstream port": lambda s: s[gw_index(s, "dialogs")]["handle"][0]["routes"][0]["handle"][1].update({"upstreams": [{"dial": "127.0.0.1:8184"}]}),
        "browser route without basic auth": lambda s: browser(s).pop(1),
        "browser proxy header literal": lambda s: browser(s)[-1]["headers"]["request"]["set"].update({"X-Watcher-Proxy-Auth": ["x" * 43]}),
        "reverse_proxy deletes X-Watcher-* wildcard": lambda s: browser(s)[-1]["headers"]["request"].update({"delete": ["X-Watcher-*"]}),
        "browser does not clear actor header": lambda s: browser(s).pop(2),
        "browser keeps basic Authorization": lambda s: browser(s).pop(5),
        "unauthenticated watcher route for /media": lambda s: s.insert(fb(s) + 1, {"group": "group21", "match": [{"path": ["/media/*"]}], "handle": [
            {"handler": "reverse_proxy", "upstreams": [{"dial": "127.0.0.1:9090"}]}]}),
    }
    caught = 0
    for name, fn in variants.items():
        cfg = mutated(fn)
        for form, candidate in (("raw", cfg), ("skeleton", caddy_skeleton(cfg))):
            ok_variant, text_variant = verdict(candidate)
            if ok_variant:
                print(f"SELFTEST_MISSED {name} ({form})")
                print(text_variant)
                return 1
        caught += 1
    # verify prints matcher summaries (INFO/FAIL): a token inside a path must come out redacted
    leak_cfg = mutated(lambda s: s.insert(0, {"match": [{"path": ["/hook/SENTINELpath0123456789/*"]}, {"path_regexp": {"name": "t", "pattern": "^/tg/SENTINELre0123456789ab/.*$"}}],
                                             "handle": [{"handler": "static_response", "status_code": 204}]}))
    ok_leak, text_leak = verdict(leak_cfg)
    assert ok_leak and "SENTINEL" not in text_leak and "/hook/<seg len=" in text_leak, ("verify printed a path token", text_leak[-600:])
    checks += 1
    # two-step, not "fail on any hit": whitelisted handlers that hit, and non-whitelisted ones that do not hit, pass
    benign = {
        "vars + map + log_append + tracing without matcher": lambda s: s.insert(0, {"handle": [{"handler": "vars", "root": "/x"}, {"handler": "map", "source": "{path}"},
                                                                                                {"handler": "log_append", "key": "k"}, {"handler": "tracing", "span": "s"}]}),
        "redir on a path that cannot hit the probes": lambda s: s.insert(0, {"match": [{"path": ["/old/*"]}], "handle": [{"handler": "static_response", "status_code": 308}]}),
        "rewrite on a case-sensitive regexp that cannot hit": lambda s: s.insert(0, {"match": [{"path_regexp": {"name": "st", "pattern": "^/static/"}}], "handle": [
            {"handler": "rewrite", "strip_path_prefix": "/static"}]}),
        "not-matcher with encode only": lambda s: s.insert(0, {"match": [{"not": [{"path": ["/x"]}]}], "handle": [{"handler": "encode", "encodings": {"zstd": {}}}]}),
        "other host before the site": lambda s: None,
    }
    for name, fn in benign.items():
        cfg = mutated(fn)
        if name == "other host before the site":
            cfg["apps"]["http"]["servers"]["srv0"]["routes"].insert(0, {"match": [{"host": ["other.example"]}], "handle": [
                {"handler": "subroute", "routes": [strip_proxy_route([{"path": ["/m/*"]}], None)]}], "terminal": True})
        ok_b, text_b = verdict(cfg)
        assert ok_b, (name, text_b)
        assert verdict(caddy_skeleton(cfg))[0], (name, "skeleton")
        checks += 1
    # before-deploy (site check S-03/S-04): no snippet yet
    base_cfg = copy.deepcopy(good)
    site = _site(base_cfg)
    site[:] = [r for r in site if not any("/m/v1/watcher" in (m.get("path_regexp") or {}).get("pattern", "") for m in (r.get("match") or []))]
    brw = site[-2]["handle"][0]["routes"][0]["handle"]
    brw[:] = [h for h in brw if not (h.get("handler") == "headers" and "request" in h)]
    brw[-1].pop("headers", None)
    ok_before, text_before = verdict(base_cfg, before_deploy=True)
    assert ok_before, text_before
    assert verdict(caddy_skeleton(base_cfg), before_deploy=True)[0], "before-deploy verify must work on the skeleton"
    for name, fn in {
        "existing /m wildcard to 8183": lambda s: s.insert(1, strip_proxy_route([{"path": ["/m/*"]}])),
        "watcher route without basic auth": lambda s: s[-2]["handle"][0]["routes"][0]["handle"].pop(1),
        "top-level rewrite before the handle blocks": lambda s: s.insert(0, {"handle": [{"handler": "rewrite", "strip_path_prefix": "/x"}]}),
        "snippet already present": lambda s: s.insert(1, copy.deepcopy(_site(good)[1])),
    }.items():
        bad = copy.deepcopy(base_cfg)
        fn(_site(bad))
        assert not verdict(bad, before_deploy=True)[0], f"before-deploy must flag: {name}"
        checks += 1
    print(f"SELFTEST_OK good_passes={passes} variants_caught={caught}/{len(variants)} (raw and skeleton) benign_two_step={len(benign)} "
          f"checks={checks} lines={len(lines)} list_format=v2 snippet_verbatim=ok re2_pins=ok caddyfile_text=ok skeleton_verify=ok before_deploy_mode=ok")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    default_paths = Path(__file__).resolve().parents[3] / "contracts/generated/caddy-watcher-gateway-paths.txt"
    snippet_help = f"snippet file (default: {SNIPPET_FILE} next to --paths)"

    p = sub.add_parser("check-artifacts")
    p.add_argument("--paths", type=Path, default=default_paths)
    p.add_argument("--snippet", type=Path, help=snippet_help)
    p.add_argument("--expect-phase-max")
    p.add_argument("--expect-yaml-sha256")
    p.set_defaults(func=cmd_check_artifacts)

    p = sub.add_parser("caddyfile-check")
    p.add_argument("--caddyfile", type=Path, required=True)
    p.set_defaults(func=cmd_caddyfile_check)

    p = sub.add_parser("verify")
    p.add_argument("--paths", type=Path, default=default_paths)
    p.add_argument("--snippet", type=Path, help=snippet_help)
    p.add_argument("--adapted", type=Path, required=True, help="JSON from `caddy adapt` or GET :2019/config/")
    p.add_argument("--host", default="jp-bot.balen.wang")
    p.add_argument("--listen-port", default="443")
    p.add_argument("--upstream", default=DEFAULT_GATEWAY_UPSTREAM)
    p.add_argument("--expect-phase-max", default="P2")
    p.add_argument("--before-deploy", action="store_true", help="site check S-03/S-04 before stage C: no snippet expected, no injection expected")
    p.add_argument("--mobile-sample", action="append")
    p.add_argument("--browser-sample", action="append")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("inventory")
    p.add_argument("--adapted", type=Path, required=True)
    p.set_defaults(func=cmd_inventory)

    p = sub.add_parser("probe", help="LOCAL non-production Caddy probe on a copy of the production Caddyfile")
    p.add_argument("--caddy", required=True, help="Caddy binary of the production version (caddy version)")
    p.add_argument("--caddyfile", type=Path, required=True, help=f"copy of the production Caddyfile; {SNIPPET_FILE} must sit next to it")
    p.add_argument("--paths", type=Path, default=default_paths)
    p.add_argument("--snippet", type=Path, help=snippet_help)
    p.add_argument("--site-address", default="jp-bot.balen.wang")
    p.add_argument("--upstream", default=DEFAULT_GATEWAY_UPSTREAM)
    p.add_argument("--watcher-upstream", default=DEFAULT_WATCHER_UPSTREAM)
    p.add_argument("--adapt-env", action="append", help="NAME=VALUE for {$NAME} adapt-time placeholders (fake values only)")
    p.add_argument("--keep", action="store_true", help="keep <copy>.o0probe")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("selftest")
    p.add_argument("--paths", type=Path, default=default_paths)
    p.add_argument("--snippet", type=Path, help=snippet_help)
    p.set_defaults(func=cmd_selftest)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
