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
                 /M/V1/WATCHER/dialogs or (wac-090) any list line's sample path in either case?
                 path: Caddy v2.10.2 MatchPath (case-insensitive; the fast */prefix/suffix
                 cases; otherwise Go path.Match with ?, [...], \\); a {placeholder}, '%', a
                 malformed glob, any non-ASCII character or a '//' (wac-092) counts as a hit;
                 path_regexp: RE2 semantics; host: Caddy MatchHost, compared only on the
                 server's own routes (site selection) - inside a site a host matcher never
                 excludes (wac-092); any other matcher (header, remote_ip, client_ip,
                 expression, not, ...) counts as a hit. wac-094 (review wac-092 🟡-1): a route
                 that misses every sample is then judged against the WHOLE prefix space
                 (_mset_may_hit_space: every parameter value, suffix and case): a concrete
                 value (…/accounts/account-a), a suffix or middle glob (media/*.png,
                 */risks/btcusdt), a glob whose literal head may start a space path, or a
                 path_regexp not anchored with '^' / with flags / with a top-level '|' / whose
                 literal prefix may start a space path, all count as a hit. wac-097 (review wac-095
                 🟡-3): the top-level '|' is found with the RE2 class rules of _re2_compile ([]…],
                 [^]…], [[:alpha:]], \\Q…\\E). wac-097 (🟡-2): a route that CHANGES THE PATH (rewrite
                 with uri / strip_path_prefix / strip_path_suffix / uri_substring / path_regexp, incl.
                 a reverse_proxy's own rewrite: the rewrite, uri, try_files, handle_path, forward_auth
                 directives, reverse_proxy { rewrite }) counts as a hit WHATEVER its matcher unless the
                 result is proven to stay outside the space AND operator-query's /v1/watcher gateway
                 space (path_change_into_space: a constant clean path outside; strip_prefix cut
                 patterns outside; strip_suffix on paths that cannot start with either prefix);
                 handle_response routes and named routes reached by invoke are followed.
                 If it hits, every handler must be one of encode, headers
                 WITHOUT "request", vars, map, log_append, tracing, and the route must carry
                 neither "terminal" nor "group";
             (d) no other route that may see the prefix (incl. /m/v1/watcherx, "\\r", "#", and
                 any path pattern written for the prefix, even a dead one behind the fallback)
                 proxies to operator-query (port 8183 on ANY host spelling: localhost, [::1],
                 tcp/127.0.0.1 ...) or the watcher (9090/9100); an unparsable dial (placeholder,
                 unix socket, port range) or dynamic_upstreams fails as UNCOMPARABLE
                 (§9.14.3 merge rule, F-10 coverage); wac-097: nor may a reverse_proxy to
                 operator-query that runs after a path change on its own chain (handle_path, uri
                 between matcher and proxy, the proxy's rewrite) unless the changed path is proven
                 outside both spaces (_rewrite_forwarder_check; watcher ports: the browser checks);
                 nested routes are followed in subroutes, handle_response and invoke -> named_routes,
                 unknown containers fail as UNCOMPARABLE, and the error chain (errors.routes,
                 handle_errors) is checked the same way (review wac-096 🟡-1);
             (e) emulated Caddy route selection per list line and method: exactly one ``/m``
                 strip, upstream 127.0.0.1:8183, Authorization and X-Watcher-* untouched, no
                 basic auth; §9.14.4 item 1 negatives never reach the gateway; the fallback
                 probes end in the fallback 404; browser samples that reach the watcher pass
                 basic auth, clear X-Watcher-* and Authorization, inject
                 ``{env.WATCHER_BROWSER_PROXY_TOKEN}``.
             Anything the emulator cannot evaluate is reported UNCOMPARABLE and fails.
  inventory  structural summary of every route (no header values, no hashes).
  probe      local (NON-production; refuses to run on a host with /srv/trader-v3) Caddy probe on a
             COPY of the candidate Caddyfile (F-12, F-13 (3)). Prints the sha256 of the copy and of
             the snippet next to it (stage C C-1 binds them to the gated candidate). Writes
             <copy>.o0probe (0600; site address -> http://<production host>:<port> (wac-092: the
             real name, every request sends Host: <production host>), admin off,
             persist_config off, auto_https off, default_bind 127.0.0.1, free http/https ports;
             the line diff is printed), adapts it and verifies the adapted JSON with the
             production upstream addresses; then RUNS a pinned copy of that JSON: only the probe
             site's server, listening on 127.0.0.1 only, every reverse_proxy dial pinned to a
             local stub (operator-query, watcher, or a sink for everything else), no other app,
             refused if anything still listens or dials elsewhere. Sends raw request lines;
             every forward carries a fake caller Authorization that must arrive unchanged.
             Literal dot segments and ``//`` (wac-092: every list line with a doubled slash at
             every position) are expected to be CLEANED and forwarded with the caller's
             Authorization untouched (never expected as 404); percent-encoded forms and ``#`` are
             forwarded raw (``#`` as %23). wac-094: every parameter line is sent again with each
             PROBE_PARAM_VALUES entry (account ids, symbols in both cases, long, dotted and image
             file names) with every method of the line. wac-097: every path change outside the
             snippet gives rewrite entry targets (rewrite_entry_targets: /x/m/v1/watcher/... for a
             strip of /x, /s/status for a replace of /s/, …), each sent with and without the fake
             Authorization; what reaches operator-query must carry exactly the caller's header.
             The .o0probe file and the temporary Caddy state dir are removed on normal exit, on
             failure, on an exception and on SIGINT/SIGTERM/SIGHUP, including a second signal
             during the cleanup (_ProbeRun); not on SIGKILL of the probe or power loss: then the
             .o0probe, the temporary dir (bcrypt hash in probe-run.json) and a running Caddy are
             left. wac-094: the temporary dir carries an owner record; every probe run first scans
             $TMPDIR (scan_probe_residue) and cleans only what such a record proves to be its own
             (reports foreign dirs and unprovable processes, never touches them), and refuses to
             run while <copy>.o0probe exists. The runbook keeps `ls -A`, `ls -d
             $TMPDIR/o0-caddy-probe-xdg-*` and `pgrep -fl o0-caddy-probe-xdg` afterwards.
  selftest   fixture-based self-test shaped like real ``caddy adapt`` output (good config +
             broken variants, raw and skeleton), artifact parser negatives, RE2 pins.

The adapted JSON contains the basic-auth bcrypt hash: keep it 0600 and never print it.
This tool never prints header values except ``{placeholder}`` names.
"""

from __future__ import annotations

import argparse
import copy
import difflib
import hashlib
import ipaddress
import json
import os
import re
import shutil
import signal
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


class _BadGlob(Exception):
    pass


def _go_scan_chunk(pattern: str) -> tuple[bool, str, str]:
    """Go path.scanChunk (go1.26 src/path/match.go)."""
    star = False
    while pattern.startswith("*"):
        pattern, star = pattern[1:], True
    inrange, i = False, 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            if i + 1 < len(pattern):
                i += 1
        elif c == "[":
            inrange = True
        elif c == "]":
            inrange = False
        elif c == "*" and not inrange:
            break
        i += 1
    return star, pattern[:i], pattern[i:]


def _go_get_esc(chunk: str) -> tuple[str, str]:
    if not chunk or chunk[0] in "-]":
        raise _BadGlob
    if chunk[0] == "\\":
        chunk = chunk[1:]
        if not chunk:
            raise _BadGlob
    rest = chunk[1:]
    if not rest:
        raise _BadGlob
    return chunk[0], rest


def _go_match_chunk(chunk: str, s: str) -> tuple[str, bool]:
    """Go path.matchChunk; a malformed chunk raises _BadGlob (Go: ErrBadPattern)."""
    failed = False
    while chunk:
        if not failed and not s:
            failed = True
        c = chunk[0]
        if c == "[":
            r = None
            if not failed:
                r, s = s[0], s[1:]
            chunk = chunk[1:]
            negated = False
            if chunk and chunk[0] == "^":
                negated, chunk = True, chunk[1:]
            match, nrange = False, 0
            while True:
                if chunk and chunk[0] == "]" and nrange > 0:
                    chunk = chunk[1:]
                    break
                lo, chunk = _go_get_esc(chunk)
                hi = lo
                if chunk[0] == "-":
                    hi, chunk = _go_get_esc(chunk[1:])
                if r is not None and lo <= r <= hi:
                    match = True
                nrange += 1
            if match == negated:
                failed = True
        elif c == "?":
            if not failed:
                if s[0] == "/":
                    failed = True
                s = s[1:]
            chunk = chunk[1:]
        else:
            if c == "\\":
                chunk = chunk[1:]
                if not chunk:
                    raise _BadGlob
            if not failed:
                if chunk[0] != s[0]:
                    failed = True
                s = s[1:]
            chunk = chunk[1:]
    return ("", False) if failed else (s, True)


def go_path_match(pattern: str, name: str) -> bool:
    """Go path.Match (``*`` never crosses '/', ``?`` one non-'/' character, ``[...]`` classes with
    ``^`` and ranges, ``\\`` escapes). Malformed pattern: raises _BadGlob (Go: ErrBadPattern)."""
    while pattern:
        star, chunk, pattern = _go_scan_chunk(pattern)
        if star and chunk == "":
            return "/" not in name
        t, ok = _go_match_chunk(chunk, name)
        if ok and (not t or pattern):
            name = t
            continue
        if star:
            advanced = False
            i = 0
            while i < len(name) and name[i] != "/":
                t, ok = _go_match_chunk(chunk, name[i + 1:])
                if ok:
                    if not pattern and t:
                        i += 1
                        continue
                    name, advanced = t, True
                    break
                i += 1
            if advanced:
                continue
        while pattern:
            _star, chunk, pattern = _go_scan_chunk(pattern)
            _go_match_chunk(chunk, "")
        return False
    return not name


GLOB_META = set("*?[]\\")


def _caddy_path_match(pattern: str, path: str) -> bool:
    """Caddy v2.10.2 MatchPath.MatchWithError (modules/caddyhttp/matchers.go) for one pattern:
    pattern and path lower-cased; ``*`` alone; the fast substring/suffix/prefix cases (their other
    characters are LITERAL there, as in Caddy); otherwise Go path.Match (``?``, ``[...]``, ``\\``).
    Raises Uncomparable where the answer depends on the request or is not modelled here:
    a ``{placeholder}`` (replaced per request, unknown ones by ""), ``%`` (compared in escaped
    space), a malformed glob (Caddy ignores the error). Callers treat Uncomparable as a hit.
    wac-092 (review wac-090 🟡-1, 🟡-2, 💭-2):
      * any non-ASCII character in the pattern or the path: Go ``strings.ToLower`` (simple case
        mapping, e.g. U+0130 'İ' -> 'i', U+212A Kelvin -> 'k') and Python ``str.lower`` (full
        mapping, 'İ' -> 'i̇'; final-sigma context) disagree, so it is not evaluated here;
      * ``//`` in the pattern: Caddy then does NOT merge doubled slashes in the request path
        (``mergeSlashes := !strings.Contains(matchPattern, "//")``), while the gateway's
        ``path_regexp`` always cleans them, so ``/m//v1/watcher/<line>`` reaches the gateway
        although no clean probe path equals it."""
    if not pattern.isascii():
        raise Uncomparable(f"path matcher {_redact_path(pattern)!r} has a non-ASCII character (Go and Python lower-case it differently)")
    if not path.isascii():
        raise Uncomparable(f"request path {_redact_path(path)!r} has a non-ASCII character")
    p = pattern.lower()
    s = path.lower()
    if "{" in p or "}" in p:
        raise Uncomparable(f"path matcher {_redact_path(pattern)!r} has a placeholder (value depends on the request)")
    if p == "*":
        return True
    if "//" in p:
        raise Uncomparable(f"path matcher {_redact_path(pattern)!r} has '//' (Caddy keeps doubled slashes of the request for it; "
                           "the gateway's path_regexp cleans them)")
    if "%" in p:
        raise Uncomparable(f"path matcher {_redact_path(pattern)!r} has '%' (Caddy compares it in escaped space)")
    stars = p.count("*")
    if stars == 2 and p.startswith("*") and p.endswith("*"):
        return p[1:-1] in s
    if stars == 1 and p.startswith("*"):
        return s.endswith(p[1:])
    if stars == 1 and p.endswith("*"):
        return s.startswith(p[:-1])
    try:
        return go_path_match(p, s)
    except _BadGlob:
        raise Uncomparable(f"path matcher {_redact_path(pattern)!r} is a malformed glob") from None


def _path_may_hit(pattern: str, path: str) -> bool:
    """F-13 step 1 for one path pattern: anything not evaluable counts as a hit."""
    try:
        return _caddy_path_match(pattern, path)
    except Uncomparable:
        return True


def _literal_head(pattern: str) -> str:
    """Lower-cased pattern up to its first glob character, placeholder, '%' or non-ASCII character
    (wac-092: a non-ASCII character may lower-case to an ASCII letter in Go, e.g. 'İ' -> 'i')."""
    for i, c in enumerate(pattern):
        if c in GLOB_META or c in "{}%" or not c.isascii():
            return pattern[:i].lower()
    return pattern.lower()


def _pattern_touches_prefix(pattern: str) -> bool:
    """A path pattern that is written for the /m/v1/watcher prefix (any case) even when no fixed
    probe hits it (e.g. a dead ``handle /m/v1/watcher/extra`` after the fallback, or
    ``/m/v1/w?tcher/...``): its literal head starts with the prefix, or it has a wildcard and its
    literal head is a non-trivial start of the prefix."""
    p = pattern.lower()
    head = _literal_head(pattern)
    if p.startswith(EXTERNAL_PREFIX) or head.startswith(EXTERNAL_PREFIX):
        return True
    return head != p and len(head) >= 2 and EXTERNAL_PREFIX.startswith(head)


# ---------------------------------------------------------------- prefix space (wac-094, review wac-092 🟡-1)
# The /m/v1/watcher prefix space: every request path the snippet (lines and fallback) can see, after Caddy
# cleans it, in ANY case: "/m/v1/watcher" itself, "/m/v1/watcher/<anything>" (every parameter value, every
# suffix) and "/m/v1/watcher\n<anything>" (the fallback's newline form). Step 1 of the shadow check used to
# evaluate a matcher against a handful of SAMPLE paths (parameters filled with 'x1'); a matcher written for one
# concrete parameter value (…/accounts/account-a), a suffix glob (media/*.png), a middle glob (*/risks/btcusdt)
# or an unanchored path_regexp (account-a$) missed every sample and was taken as "cannot hit" although real
# Caddy applied it to table paths. Now a matcher counts as a hit when it MAY select ANY path of the space.
_SPACE_SEPARATORS = ("/", "\n")
# wac-097 (review wac-096 🔴-1, coordinator): a path CHANGED on the way to operator-query is also judged against
# operator-query's own gateway space: the prefix middleware triggers on /v1/watcher itself or /v1/watcher/..., /v1/watcher%...
# (any ASCII case, backend-api.md §9.3 "触发条件"); '\n' is added to stay on the safe side.
OQ_GATEWAY_PREFIX = APP_OUTER_PREFIX                        # /v1/watcher
_OQ_SEPARATORS = ("/", "%", "\n")
_SPACES = ((EXTERNAL_PREFIX, _SPACE_SEPARATORS), (OQ_GATEWAY_PREFIX, _OQ_SEPARATORS))


def _in_space(path: str, prefix: str = EXTERNAL_PREFIX, seps: tuple = _SPACE_SEPARATORS) -> bool:
    """Is this (lower-cased, literal) path inside the prefix space (default: /m/v1/watcher)?"""
    p = path.lower()
    return p == prefix or any(p.startswith(prefix + s) for s in seps)


def _head_meets_space(head: str, prefix: str = EXTERNAL_PREFIX, seps: tuple = _SPACE_SEPARATORS) -> bool:
    """Can some path of the prefix space START with this literal (lower-cased) head?"""
    h = head.lower()
    return any((prefix + s).startswith(h) for s in seps) or _in_space(h, prefix, seps)


def _in_any_space(path: str) -> bool:
    """/m/v1/watcher space or operator-query's /v1/watcher gateway space (wac-097)."""
    return any(_in_space(path, pfx, seps) for pfx, seps in _SPACES)


def _path_pattern_may_hit_space(pattern: str, prefix: str = EXTERNAL_PREFIX, seps: tuple = _SPACE_SEPARATORS) -> bool:
    """F-13 step 1 for one ``path`` pattern against the WHOLE prefix space, with Caddy v2.10.2 MatchPath
    structure: anything _caddy_path_match cannot evaluate (non-ASCII, placeholder, '//', '%', malformed
    glob) is a hit; '*' alone hits; ``*x*`` (substring) and ``*x`` (suffix) always hit (some parameter value
    or suffix of a table path contains / ends with x: media/*.png, */risks/btcusdt); ``x*`` (fast prefix, x
    literal) hits when a space path can start with x; a pattern without glob characters is an exact path and
    hits when it lies in the space (…/accounts/account-a); any other glob hits unless its literal head (up to
    the first glob character) rules the whole space out (…/accounts/a*, */x, ?x, [x]… all hit)."""
    try:
        _caddy_path_match(pattern, EXTERNAL_PREFIX)
    except Uncomparable:
        return True
    p = pattern.lower()
    if p == "*":
        return True
    stars = p.count("*")
    if stars == 2 and p.startswith("*") and p.endswith("*"):
        return True
    if stars == 1 and p.startswith("*"):
        return True
    if stars == 1 and p.endswith("*"):
        return _head_meets_space(p[:-1], prefix, seps)
    head = _literal_head(p)
    if head == p:
        return _in_space(p, prefix, seps)
    return head == "" or _head_meets_space(head, prefix, seps)


_RE_META = set("\\.+*?()|[]{}^$")


def _re_class_end(pattern: str, i: int) -> int | None:
    """Index just after the character class that opens at ``pattern[i] == '['``, with the class rules of
    _re2_compile / Go regexp/syntax ``parseClass`` (wac-097, review wac-095 🟡-3): an optional '^'; a ']' right
    after '[' or '[^' is a LITERAL member (``[]…]``, ``[^]…]``); '\\' escapes the next character; '[:' up to the
    next ':]' is a POSIX class (``[[:alpha:]]``) whose ']' does not close; the first other ']' closes. None when
    the class never closes (Go rejects the pattern; the caller counts it as a hit)."""
    j = i + 1
    if pattern[j:j + 1] == "^":
        j += 1
    if pattern[j:j + 1] == "]":
        j += 1
    while j < len(pattern):
        c = pattern[j]
        if c == "\\":
            j += 2
            continue
        if pattern.startswith("[:", j):
            end = pattern.find(":]", j + 2)
            if end >= 0:
                j = end + 2
                continue
        if c == "]":
            return j + 1
        j += 1
    return None


def _re_top_level_alternation(pattern: str) -> bool:
    """Has this RE2 pattern a '|' outside every group? Character classes are skipped with _re_class_end, ``\\Q…\\E``
    is literal text up to ``\\E`` (or the end of the pattern), any other '\\' escapes one character. wac-097: the
    wac-094 scanner closed ``[](]`` at its first ']' and counted the '(' as a group, so the top-level '|' of
    ``^/static[](]|account-a$`` looked nested (g01c, g04c, g04d and 54 black-box patterns of review wac-095
    passed verify although real Caddy injected). An unbalanced ')' or an unclosed class counts as top level."""
    depth, i = 0, 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            if pattern[i + 1:i + 2] == "Q":
                end = pattern.find("\\E", i + 2)
                if end < 0:
                    return False
                i = end + 2
                continue
            i += 2
            continue
        if c == "[":
            end = _re_class_end(pattern, i)
            if end is None:
                return True
            i = end
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "|" and depth <= 0:
            return True
        i += 1
    return False


def _regexp_literal_head(pattern: str) -> tuple[str, bool] | None:
    """(literal prefix after '^' in lower case, whether the pattern is exactly that literal path) or None when the
    pattern is not anchored with a leading '^', carries flags, has a top-level alternation, or its literal prefix
    is non-ASCII or has '%' (then nothing about the paths it selects is known)."""
    if not pattern.startswith("^") or re.search(r"\(\?[a-zA-Z-]", pattern):
        return None
    if _re_top_level_alternation(pattern):
        return None
    lit, i = "", 1
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern) and not pattern[i + 1].isalnum():
            lit += pattern[i + 1]
            i += 2
            continue
        if c in _RE_META:
            break
        lit += c
        i += 1
    rest = pattern[i:]
    if rest[:1] in ("?", "*", "+", "{"):
        lit = lit[:-1]
    if not lit.isascii() or "%" in lit:
        return None
    return lit.lower(), rest == "$"


def _regexp_may_hit_space(pattern: str, prefix: str = EXTERNAL_PREFIX, seps: tuple = _SPACE_SEPARATORS) -> bool:
    """F-13 step 1 for one ``path_regexp`` (RE2, case-sensitive, not multi-line) against the prefix space.
    It can only EXCLUDE the space when it is anchored with a leading '^', carries no flags ('(?i)', '(?m)',
    '(?s)', '(?U)'), has no top-level alternation ('^/a|watcher' also matches any path containing
    'watcher'; classes and ``\\Q…\\E`` are skipped by the RE2 rules, wac-097), and its literal prefix after '^'
    (up to the first regexp metacharacter; a '\\' before a punctuation character is that character; a
    quantifier drops the character before it), compared without case, rules the whole space out. Everything
    else counts as a hit (``account-a$``, ``%2[fF]``, ``^.*watcher``, ``^/(m|x)/``, ``^/static[](]|x$``...)."""
    head = _regexp_literal_head(pattern)
    if head is None:
        return True
    lit, exact = head
    if exact:                  # exact literal path
        return _in_space(lit, prefix, seps)
    return _head_meets_space(lit, prefix, seps)


def _regexp_may_start_prefix(pattern: str, prefix: str = EXTERNAL_PREFIX) -> bool:
    """May this path_regexp select a path that STARTS (any case) with the prefix (inside the space or not,
    e.g. /m/v1/watcherx)? Same parse as _regexp_may_hit_space."""
    head = _regexp_literal_head(pattern)
    if head is None:
        return True
    lit, exact = head
    if exact:
        return lit.startswith(prefix)
    return lit.startswith(prefix) or prefix.startswith(lit)


def _mset_may_hit_space(mset: dict, host: str, host_exact: bool = True) -> bool:
    """F-13 step 1 over the whole prefix space: path and path_regexp by the rules above, host as in
    _mset_may_hit (compared only at the server's top level), every other matcher kind counts as a hit."""
    for kind, value in mset.items():
        if kind == "path":
            if not any(_path_pattern_may_hit_space(str(p)) for p in value):
                return False
        elif kind == "path_regexp":
            if not _regexp_may_hit_space(str(value.get("pattern", ""))):
                return False
        elif kind == "host":
            if _host_may_exclude(value, host, host_exact):
                return False
    return True


def _may_hit_space(match: list | None, host: str, host_exact: bool = True) -> bool:
    if not match:
        return True
    return any(_mset_may_hit_space(m, host, host_exact) for m in match)


SPACE_LABEL = "the /m/v1/watcher prefix space (some parameter value, suffix or case of a table path)"


# ---------------------------------------------------------------- path changes (wac-097, review wac-095 🟡-2)
# A matcher decides which requests a route's handlers see; a handler that CHANGES THE PATH decides what every later
# handler and route sees. Review wac-095 on real Caddy v2.10.2: `@s path /x/*` + `request_header @s <observer>` +
# `uri @s strip_prefix /x` adapt into ONE route [rewrite, headers] (g16): `GET /x/m/v1/watcher/trading/accounts`
# misses the prefix space by its matcher, leaves the route as /m/v1/watcher/trading/accounts with the injected token,
# and the snippet forwards it. The same with `uri @s replace /s/ /m/v1/watcher/ 1` (g17), and `handle_path /x/*`
# strips between its matcher and a reverse_proxy to operator-query (g18). So a path-changing handler - `rewrite` with
# uri, strip_path_prefix, strip_path_suffix, uri_substring or path_regexp (the `rewrite`, `uri`, `try_files`,
# `handle_path` directives), and the `rewrite` of a reverse_proxy - counts as "may put the path into the prefix
# space" WHATEVER its matcher, unless the tool can prove the result stays outside:
#   * uri: its path part (Caddy rewrite.go:167-199: up to the first '?' or '#') is a constant clean absolute path
#     (_constant_path) outside the space, e.g. `rewrite /healthz /api/status`; a uri without a path part ("?a=b")
#     does not change the path;
#   * strip_path_prefix P (constant): Caddy cleans the path, then trims P without case (rewrite.go:259-268,
#     trimPathPrefix). The cleaned result is the path itself (P absent, or an encoded '..' popped it again) or the path
#     with P cut off: for every path pattern the cut pattern is added when its literal head starts with P; a pattern
#     whose literal head is a proper start of P ('/*', '/a*'), a suffix/substring glob or a path_regexp: unknown;
#   * strip_path_suffix S (constant, no '%', no '//', and either no '.' at all or no '/' and not only dots): the
#     cleaned result is the cleaned path with trailing segments cut (and at most one more '..' pop), i.e. a start of
#     it; when no possible path can START with /m/v1/watcher (any case), no start of it lies in the space;
#   * anything else (a placeholder, uri_substring, path_regexp, strip on unknown paths): may enter.
# The possible paths are a list of items ("path", Caddy path pattern) | ("re", RE2 pattern) | _OUTSIDE (cannot start
# with /m/v1/watcher), or None (any path). A matcher replaces them by its own path / path_regexp items (the current
# path must match it, so they are a superset of the intersection).
_PATH_CHANGE_FIELDS = ("uri", "strip_path_prefix", "strip_path_suffix", "uri_substring", "path_regexp")
_OUTSIDE = ("outside", "")
_CONST_PATH_BAD = set(GLOB_META) | set("{}%#?")


def _uri_path_part(uri: str) -> str:
    """The path part of a rewrite ``uri`` as Caddy v2.10.2 splits it: up to the first '?' or '#'; empty when the uri
    starts with '?' or '#' (then the path is not changed)."""
    for i, c in enumerate(uri):
        if c in "?#":
            return uri[:i]
    return uri


def _constant_path(value: str) -> str | None:
    """``value`` when a rewrite sets it literally as the path and every later matcher (which cleans the path) sees
    exactly it: absolute, printable ASCII without whitespace, no placeholder, '%', glob character, backslash, '#',
    '?', '//' or dot segment. None otherwise."""
    if not value.startswith("/") or not value.isascii() or "//" in value:
        return None
    if any(c in _CONST_PATH_BAD or not c.isprintable() or c.isspace() for c in value):
        return None
    if any(seg in (".", "..") for seg in value.split("/")):
        return None
    return value


def _path_pattern_may_start_prefix(pattern: str, prefix: str = EXTERNAL_PREFIX) -> bool:
    """May a Caddy ``path`` pattern select a path that STARTS (any case) with the prefix?"""
    try:
        _caddy_path_match(pattern, prefix)
    except Uncomparable:
        return True
    p = pattern.lower()
    if p.startswith("*"):
        return True
    head = _literal_head(p)
    if head == p:
        return p.startswith(prefix)
    return head.startswith(prefix) or prefix.startswith(head)


def _item_may_hit_space(item: tuple[str, str]) -> bool:
    """In the /m/v1/watcher space OR operator-query's /v1/watcher gateway space (wac-097: a changed path may reach
    operator-query through any later route, e.g. a panel /v1/* forwarder, or be forwarded by the changing route)."""
    kind, value = item
    for pfx, seps in _SPACES:
        if kind == "path" and _path_pattern_may_hit_space(value, pfx, seps):
            return True
        if kind == "re" and _regexp_may_hit_space(value, pfx, seps):
            return True
    return False


def _item_may_start_prefix(item: tuple[str, str]) -> bool:
    kind, value = item
    for pfx, _seps in _SPACES:
        if kind == "path" and _path_pattern_may_start_prefix(value, pfx):
            return True
        if kind == "re" and _regexp_may_start_prefix(value, pfx):
            return True
    return False


def _paths_may_hit_space(paths: list | None) -> bool:
    return paths is None or any(_item_may_hit_space(i) for i in paths)


def _paths_union(a: list | None, b: list | None) -> list | None:
    if a is None or b is None:
        return None
    return a + [i for i in b if i not in a]


def _paths_after_match(paths: list | None, match: list | None) -> tuple[list | None, bool]:
    """(possible paths inside a route with this matcher, whether the matcher itself constrains the path)."""
    if not match:
        return paths, False
    items: list = []
    constrained = True
    for mset in match:
        if "path" in mset:
            items += [("path", str(p)) for p in mset.get("path") or []]
        elif "path_regexp" in mset:
            items.append(("re", str((mset.get("path_regexp") or {}).get("pattern", ""))))
        else:
            constrained = False
            if paths is None:
                return None, False
            items += [i for i in paths if i not in items]
    return items, constrained


def _strip_prefix_paths(paths: list | None, prefix: str) -> list | None:
    """Possible paths after ``strip_path_prefix prefix``. Caddy trims P from the DECODED path text but only cleans the
    ESCAPED path first, so an encoded dot segment survives the trim and is resolved later by the matchers: with
    P = /m/v1, ``/m/v1/%2e%2e/m/v1/watcher/status`` is matched as /m/m/v1/watcher/status and leaves the strip as
    /../m/v1/watcher/status = /m/v1/watcher/status (found by the wac-097 real-Caddy differential). In cleaned terms
    the result is the path with SOME segment-prefix of P removed (none, /m, /m/v1): every such cut is added, and
    the full text of P for a P that ends inside a segment. A cut remainder that starts with '.', or a P with a dot
    segment, '%', a placeholder, a glob character or '//', is unknown (None)."""
    if paths is None:
        return None
    p = prefix if prefix.startswith("/") else "/" + prefix
    if not p.isascii() or "//" in p or any(c in GLOB_META or c in "{}%" or c.isspace() for c in p):
        return None
    segs = p.strip("/").split("/") if p.strip("/") else []
    if any(s in (".", "..") for s in segs):
        return None
    cuts = ["/" + "/".join(segs[:j]).lower() for j in range(1, len(segs) + 1)]
    if p.lower() not in cuts:
        cuts.append(p.lower())
    out = list(paths)
    for kind, value in paths:
        if kind != "path":
            return None
        try:
            _caddy_path_match(value, "/")
        except Uncomparable:
            return None
        v = value.lower()
        if v.startswith("*"):
            return None
        head = _literal_head(v)
        for pl in cuts:
            if head.startswith(pl):
                rest = v[len(pl):]
                if rest.startswith("."):
                    return None
                cut = ("path", rest if rest.startswith("/") else "/" + rest)
                if cut not in out:
                    out.append(cut)
            elif head != v and pl.startswith(head):
                return None
    return out


def _strip_suffix_safe(suffix: str) -> bool:
    s = suffix
    if not s or not s.isascii() or "//" in s or any(c in "{}%\\" or c.isspace() or not c.isprintable() for c in s):
        return False
    if "." not in s:
        return True
    return "/" not in s and s.strip(".") != ""


def _paths_after_rewrite(spec: dict, paths: list | None) -> tuple[list | None, list[str]]:
    """(possible paths after a rewrite handler, or a reverse_proxy's ``rewrite``; the path-changing fields that
    applied). Caddy's order: uri, strip_path_prefix, strip_path_suffix, uri_substring, path_regexp."""
    changed: list[str] = []
    for f in _PATH_CHANGE_FIELDS:
        value = spec.get(f)
        if value in (None, "", []):
            continue
        if f == "uri":
            # wac-097 (review wac-096 second round 🔴-A, coordinator): a uri whose path part is EMPTY ('?a=1', '#frag')
            # does not change the path - Caddy forwards the original (or an earlier rewritten) path; it is never a
            # proof: counted as unknown ("may hit"), like any non-constant path
            part = _uri_path_part(str(value))
            lit = _constant_path(part) if part else None
            paths = None if lit is None else [("path", lit)]
        elif f == "strip_path_prefix":
            paths = _strip_prefix_paths(paths, str(value))
        elif f == "strip_path_suffix":
            ok = _strip_suffix_safe(str(value)) and paths is not None and not any(_item_may_start_prefix(i) for i in paths)
            paths = [_OUTSIDE] if ok else None
        else:
            paths = None
        changed.append(f)
    return paths, changed


def _path_changes(handlers: list, paths: list | None, out: list, named: dict | None = None, stack: tuple = ()) -> list | None:
    """Follow the possible paths through a handler list; append (handler kind, fields, paths after) for every path
    change. Containers followed (wac-097, review wac-096 🟡-1): subroute routes; a reverse_proxy's own ``rewrite``
    (forward_auth, ``reverse_proxy { rewrite … }``: only the upstream COPY is rewritten, recorded, never carried on)
    and its ``handle_response[].routes`` (they run with the ORIGINAL request - the paths before that rewrite, review
    wac-096 second round 🔴-B - and, when they do not respond, forward_auth's 2xx branch, the chain continues); ``invoke`` -> the server's ``named_routes`` in the caller's context. An
    unknown or recursive named route, or any other handler carrying nested routes, is recorded as an unknown change
    (paths None). Returns the possible paths after the list."""
    for h in handlers or []:
        kind = h.get("handler")
        if kind == "subroute":
            paths = _route_list_changes(h.get("routes") or [], paths, out, named, stack)
            if _nested_route_keys(h):        # e.g. a subroute's own 'errors' routes
                out.append((str(kind), ["nested routes this tool does not enumerate"], None))
                paths = None
        elif kind == "rewrite":
            new, fields = _paths_after_rewrite(h, paths)
            if fields:
                out.append((kind, fields, new))
                paths = new
        elif kind in ("reverse_proxy", "intercept"):
            if kind == "reverse_proxy" and isinstance(h.get("rewrite"), dict):
                new, fields = _paths_after_rewrite(h["rewrite"], paths)
                if fields:
                    out.append(("reverse_proxy rewrite", fields, new))
            # handle_response routes (reverse_proxy, and intercept - wac-099 🔴-2) run with the ORIGINAL request
            for hr in h.get("handle_response") or []:
                paths = _route_list_changes(hr.get("routes") or [], paths, out, named, stack)
            if _nested_route_keys(h):
                out.append((str(kind), ["nested routes this tool does not enumerate"], None))
                paths = None
        elif kind == "invoke":
            name = str(h.get("name", ""))
            route = (named or {}).get(name)
            if not isinstance(route, dict) or name in stack:
                out.append((f"invoke {name!r}", ["unknown or recursive named route"], None))
                paths = None
            else:
                paths = _route_list_changes([route], paths, out, named, stack + (name,))
        elif _nested_route_keys(h):
            out.append((str(kind), ["nested routes this tool does not enumerate"], None))
            paths = None
    return paths


def _route_list_changes(routes: list, paths: list | None, out: list, named: dict | None, stack: tuple) -> list | None:
    cur = paths
    for r in routes or []:
        inner, _c = _paths_after_match(cur, r.get("match"))
        cur = _paths_union(cur, _path_changes(r.get("handle") or [], inner, out, named, stack))
    return cur


def path_change_into_space(route: dict, named: dict | None = None) -> str | None:
    """F-13 step 1 addition (wac-097): the first path change of this route (containers as in _path_changes) whose
    result may lie in the /m/v1/watcher prefix space or operator-query's /v1/watcher gateway space, described; None
    when the route changes no path or every change is proven to stay outside both."""
    changes: list = []
    start, _c = _paths_after_match(None, route.get("match"))
    _path_changes(route.get("handle") or [], start, changes, named)
    for kind, fields, after in changes:
        if _paths_may_hit_space(after):
            return (f"the /m/v1/watcher prefix space after a path change ({kind} {'+'.join(fields)}; not proven to stay "
                    f"outside it and operator-query's /v1/watcher space)")
    return None


def _host_match(hosts: list, host: str) -> bool:
    """Caddy v2.10.2 MatchHost.MatchWithError (modules/caddyhttp/matchers.go) for a request Host
    without port: a pattern with '*' is compared label by label (same number of labels; a label
    that is exactly '*' matches any one label, any other label compares case-insensitively and
    LITERALLY, so 'jp-*' only matches 'jp-*'); otherwise case-insensitive equality.
    Raises Uncomparable (wac-092, review wac-090 🟡-3) for a ``{placeholder}`` (replaced per
    request: '{http.request.host}' matches every host) and for a non-ASCII name (Caddy converts
    it with IDNA). Callers in the F-13 step 1 treat Uncomparable as a hit."""
    req = host.lower()
    for h in hosts:
        h = str(h)
        if "{" in h or "}" in h:
            raise Uncomparable(f"host matcher {h!r} has a placeholder (value depends on the request)")
        if not h.isascii():
            raise Uncomparable("host matcher has a non-ASCII name (IDNA conversion is not modelled here)")
        h = h.lower()
        if "*" in h:
            want, got = h.split("."), req.split(".")
            if len(want) == len(got) and all(w == "*" or w == g for w, g in zip(want, got)):
                return True
        elif h == req:
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
    """Emulated rewrite: strip_path_prefix, strip_path_suffix and (wac-097, review wac-095 💭-4) a ``uri`` whose path
    part is a constant clean absolute path (``rewrite /healthz /api/status``; a uri without a path part keeps the
    path). Anything else (placeholders, uri_substring, path_regexp, method, query) is UNCOMPARABLE."""
    keys = set(handler) - {"handler"}
    if not keys <= {"strip_path_prefix", "strip_path_suffix", "uri"}:
        raise Uncomparable(f"rewrite fields {sorted(keys)}")
    if "uri" in handler:
        part = _uri_path_part(str(handler.get("uri") or ""))
        if part:
            lit = _constant_path(part)
            if lit is None:
                raise Uncomparable(f"rewrite uri path {_redact_path(part)!r} is not a constant clean absolute path")
            req.path = lit
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
            if kind == "invoke":      # wac-097: named routes are not emulated
                raise Uncomparable(f"invoke {str(handler.get('name'))!r} (named routes are not emulated)")
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
    return [str(u.get("dial", "")) for u in handler.get("upstreams", []) or []]


def dial_endpoint(dial: str) -> tuple[str, str]:
    """(host class, port) of a reverse_proxy dial. Loopback spellings (``localhost``, ``127.0.0.0/8``,
    ``::1``, ``[::1]``, ``::ffff:127.x``, an empty host, ``0.0.0.0``/``::`` which Go dials locally)
    all become ``loopback``. Raises Uncomparable for a placeholder, a non-tcp network (unix socket,
    udp, ...), a missing or non-numeric port (range) or an unparsable address."""
    d = str(dial).strip()
    if not d or "{" in d or "}" in d:
        raise Uncomparable(f"upstream dial {d!r} is empty or has a placeholder")
    if "/" in d:
        network, _, d = d.partition("/")
        if network.lower() not in ("tcp", "tcp4", "tcp6"):
            raise Uncomparable(f"upstream dial network {network!r} (unix socket or non-tcp)")
    m = re.fullmatch(r"\[([^\]]*)\]:([^:]*)|([^:\[\]]*):([^:]*)", d)
    if not m:
        raise Uncomparable(f"upstream dial {d!r} is not host:port")
    host, port = (m.group(1), m.group(2)) if m.group(1) is not None else (m.group(3), m.group(4))
    if not re.fullmatch(r"[0-9]{1,5}", port or ""):
        raise Uncomparable(f"upstream dial {d!r}: port {port!r} is not a single number")
    h = host.lower().rstrip(".")
    if h in ("", "localhost") or h.endswith(".localhost"):
        return "loopback", str(int(port))
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return h, str(int(port))
    mapped = getattr(ip, "ipv4_mapped", None)
    if ip.is_loopback or ip.is_unspecified or (mapped is not None and (mapped.is_loopback or mapped.is_unspecified)):
        return "loopback", str(int(port))
    return str(ip), str(int(port))


def dial_port(dial: str) -> str | None:
    try:
        return dial_endpoint(dial)[1]
    except Uncomparable:
        return None


def same_endpoint(dial: str, upstream: str) -> bool | None:
    """True/False when both parse; None (unknown) when the dial does not."""
    try:
        return dial_endpoint(dial) == dial_endpoint(upstream)
    except Uncomparable:
        return None


def _is_watcher_dial(dial: str) -> bool:
    return dial_port(dial) in WATCHER_PORTS


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


def _walk_all_routes(server: dict):
    """(trail, route) of every route of a server in every container (wac-097, review wac-096 🟡-1): routes, subroute
    routes, reverse_proxy handle_response routes, the error chain (``errors.routes``) and ``named_routes``. Trails of
    the main chain are the same int tuples as _walk yields."""
    def rec(routes: list, trail: tuple):
        for i, r in enumerate(routes or []):
            here = trail + (i,)
            yield here, r
            for h in r.get("handle") or []:
                if h.get("handler") == "subroute":
                    yield from rec(h.get("routes") or [], here)
                elif h.get("handler") in ("reverse_proxy", "intercept"):
                    for j, hr in enumerate(h.get("handle_response") or []):
                        yield from rec(hr.get("routes") or [], here + (f"handle_response{j}",))
    yield from rec(server.get("routes") or [], ())
    yield from rec((server.get("errors") or {}).get("routes") or [], ("errors",))
    for name, r in sorted((server.get("named_routes") or {}).items()):
        if isinstance(r, dict):
            yield from rec([r], (f"named:{name}",))


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


def _host_may_exclude(value: list, host: str, host_exact: bool) -> bool:
    """F-13 step 1 for a host matcher. Only at the server's top level (the Caddyfile's site
    selection, host_exact=True) is the host compared, with Caddy's MatchHost semantics and
    placeholders counting as a hit. Inside a site (host_exact=False) a host matcher NEVER excludes
    (wac-092, review wac-090 🟡-3): the site may answer to several names, the Host header is the
    caller's choice, and the local probe cannot reproduce every name; so a host-conditioned route
    that runs before the snippet must pass the F-13 whitelist like any other."""
    if not host_exact:
        return False
    try:
        return not _host_match(value, host)
    except Uncomparable:
        return False


def _mset_may_hit(mset: dict, path: str, host: str, host_exact: bool = True) -> bool:
    """F-13 step 1: may this matcher set match the probe? Only path, path_regexp (RE2) and host (at
    the server's top level only, see _host_may_exclude) can exclude; method/protocol/not/expression/
    header/remote_ip/client_ip/any other or unknown matcher counts as a hit."""
    for kind, value in mset.items():
        if kind == "path":
            if not any(_path_may_hit(p, path) for p in value):
                return False
        elif kind == "path_regexp":
            try:
                if not re2_search(value.get("pattern", ""), path):
                    return False
            except Uncomparable:
                pass
        elif kind == "host":
            if _host_may_exclude(value, host, host_exact):
                return False
    return True


def _may_hit(match: list | None, path: str, host: str, host_exact: bool = True) -> bool:
    if not match:
        return True
    return any(_mset_may_hit(m, path, host, host_exact) for m in match)


def _host_excludes_route(route: dict, host: str, host_exact: bool) -> bool:
    """Every matcher set of the route has a host matcher that rules the site host out (server top level only)."""
    match = route.get("match")
    return bool(match) and all("host" in m and _host_may_exclude(m["host"], host, host_exact) for m in match)


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


def shadow_probes(lines: list[Line]) -> tuple[str, ...]:
    """F-13's three probes, then every list line's sample path as written and upper-cased (wac-090:
    a handler that only hits one table path, e.g. ``request_header /m/v1/w?tcher/trading/accounts``,
    must reach step 2 as well)."""
    extra = [s for line in lines for s in (line.sample(), line.sample().upper())]
    return SHADOW_PROBES + tuple(dict.fromkeys(p for p in extra if p not in SHADOW_PROBES))


def _shadow_check(rep: Report, levels: list[tuple], host: str, label: str, probes: tuple[str, ...] = SHADOW_PROBES,
                  named: dict | None = None) -> None:
    """F-13 two-step check on every route that runs before the anchor. levels = [(list, index, handler_index|None), ...]
    from the server's route list down to the list that holds the anchor (handler_index None on the last level)."""
    checked = 0
    for depth, (routes, index, hi) in enumerate(levels):
        for j in range(index):
            route = routes[j]
            # depth 0 = the server's route list (site selection by host); deeper = inside the site
            hits = [p for p in probes if _may_hit(route.get("match"), p, host, host_exact=depth == 0)]
            # wac-094 (review wac-092 🟡-1): samples are not the space; a matcher that misses every sample may
            # still select another parameter value, suffix or case of a table path
            if not hits and _may_hit_space(route.get("match"), host, host_exact=depth == 0):
                hits = [SPACE_LABEL]
            # wac-097 (review wac-095 🟡-2): whatever the matcher, a handler that changes the path may hand the
            # snippet a prefix-space path (with whatever the route injected) unless the result is proven outside
            if not hits and not _host_excludes_route(route, host, depth == 0):
                moved = path_change_into_space(route, named)
                if moved:
                    hits = [moved]
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


def _mset_written_for_prefix(mset: dict, host: str, host_exact: bool = True) -> bool:
    if "host" in mset and _host_may_exclude(mset["host"], host, host_exact):
        return False
    if any(_pattern_touches_prefix(str(p)) for p in mset.get("path") or []):
        return True
    # wac-094: a path or path_regexp matcher that may select any path of the prefix space (``*.png``,
    # ``*/risks/x``, an unanchored regexp) forwards table paths too
    return ("path" in mset or "path_regexp" in mset) and _mset_may_hit_space(mset, host, host_exact)


def _forwarder_check(rep: Report, routes: list, probes: list[str], host: str, upstream: str, skip: set[int], trail: tuple = (),
                     inherited: bool = False, named: dict | None = None, stack: tuple = (), chain: str = "",
                     oq_only: bool = False) -> None:
    """§9.14.3 merge rule + F-10: no route but the snippet may proxy the prefix to operator-query or the
    watcher. Operator-query is recognised by the PORT of ``upstream`` on any host, the watcher by
    9090/9100 (``localhost:8183``, ``[::1]:8183``, ``tcp/127.0.0.1:8183`` ... are all caught); a
    reverse_proxy whose dial cannot be parsed (placeholder, unix socket, range) or that has
    dynamic_upstreams fails as UNCOMPARABLE. A route "may see the prefix" when a probe may hit it
    or when one of its path patterns is written for the prefix (_pattern_touches_prefix), so dead
    forwarders behind the fallback fail too. wac-097 (review wac-096 🟡-1): nested routes are followed in every
    container - subroute routes, a reverse_proxy's ``handle_response[].routes``, ``invoke`` -> the server's
    ``named_routes`` (in the caller's context; an unknown or recursive name fails as UNCOMPARABLE) - and any other
    handler that carries nested routes fails as UNCOMPARABLE; run_verify also runs this on the server's
    ``errors.routes`` (handle_errors: a failing forward_auth or basic_auth sends the request there).
    wac-099 🔴-1: a route whose matcher misses the prefix but whose handlers CHANGE THE PATH into (possibly) the prefix
    space or operator-query's /v1/watcher space (path_change_into_space) is not skipped: its nested routes are judged
    with every prefix probe as if the prefix arrived (``oq_only``: there only operator-query forwarders fail - a
    stripped browser path to the watcher behind basic auth is the browser checks' business). Unknown containers are
    refused by _container_check whatever the matcher (wac-099 🔴-2)."""
    gw_port = dial_endpoint(upstream)[1]
    for index, route in enumerate(routes):
        if id(route) in skip:
            continue
        match = route.get("match")
        exact = not trail   # host compared only for the server's own routes (site selection); wac-092
        hits = [p for p in probes if _may_hit(match, p, host, exact)]
        written = any(_mset_written_for_prefix(m, host, exact) for m in match or [])
        route_oq_only = oq_only
        moved = None
        if not (hits or written or (inherited and not match)):
            if exact and _host_excludes_route(route, host, True):
                continue
            moved = path_change_into_space(route, named)
            if not moved:
                continue
            route_oq_only = True
        what = repr(hits[0]) if hits else (repr(moved) if moved else "the /m/v1/watcher prefix (path pattern written for it)")
        here = trail + (index,)
        where = f"{chain}route {'.'.join(map(str, here))} ({_route_matcher_summary(match)})"
        nested_inherited = bool(written or inherited or moved) and not hits
        nested_probes = list(PREFIX_PROBES) if moved else hits

        def nested(sub_routes: list, sub_trail: tuple, sub_stack: tuple) -> None:
            _forwarder_check(rep, sub_routes or [], nested_probes, host, upstream, skip, sub_trail, inherited=nested_inherited,
                             named=named, stack=sub_stack, chain=chain, oq_only=route_oq_only)

        for h in route.get("handle", []) or []:
            kind = h.get("handler")
            if kind == "subroute":
                nested(h.get("routes", []) or [], here, stack)
            elif kind == "invoke":
                name = str(h.get("name", ""))
                target = (named or {}).get(name)
                if not isinstance(target, dict) or name in stack:
                    rep.fail(f"UNCOMPARABLE {where} may pass {what} to named route {name!r} (unknown or recursive invoke)")
                else:
                    nested([target], here + (f"invoke:{name}",), stack + (name,))
            elif kind in ("reverse_proxy", "intercept"):
                for i, hr in enumerate(h.get("handle_response") or []):
                    nested(hr.get("routes") or [], here + (f"handle_response{i}",), stack)
                if kind == "intercept":
                    continue
                dials = _dials(h)
                if "dynamic_upstreams" in h or not dials:
                    rep.fail(f"UNCOMPARABLE {where} may forward {what} with dynamic or no static upstreams")
                    continue
                for d in dials:
                    try:
                        _host_class, port = dial_endpoint(d)
                    except Uncomparable as exc:
                        rep.fail(f"UNCOMPARABLE {where} may forward {what}: {exc}")
                        continue
                    if port == gw_port or (port in WATCHER_PORTS and not route_oq_only):
                        rep.fail(f"{where} may forward {what} to {d} (port {port}): only the snippet may route the "
                                 "/m/v1/watcher prefix to operator-query or the watcher (§9.14.3, F-10)")


def _container_check(rep: Report, server: dict) -> int:
    """wac-099 🔴-2: every handler anywhere in the server (routes, subroute routes, reverse_proxy / intercept
    handle_response routes, errors.routes, every named route) that carries nested routes or handlers this tool does not
    follow fails as UNCOMPARABLE, whatever its matcher and whether a path was changed before it. Returns the number of
    handlers inspected."""
    seen = 0

    def rec(routes: list, trail: tuple) -> None:
        nonlocal seen
        for i, r in enumerate(routes or []):
            here = trail + (i,)
            for h in r.get("handle") or []:
                seen += 1
                bad = _nested_route_keys(h)
                if bad:
                    rep.fail(f"UNCOMPARABLE route {'.'.join(map(str, here))} ({_route_matcher_summary(r.get('match'))}): handler "
                             f"{h.get('handler')!r} carries nested routes or handlers under {bad} that this tool does not follow "
                             "(only subroute routes, reverse_proxy/intercept handle_response routes, invoke -> named routes and "
                             "errors routes are followed)")
                if h.get("handler") == "subroute":
                    rec(h.get("routes") or [], here)
                elif h.get("handler") in ("reverse_proxy", "intercept"):
                    for j, hr in enumerate(h.get("handle_response") or []):
                        rec(hr.get("routes") or [], here + (f"handle_response{j}",))

    rec(server.get("routes") or [], ())
    rec((server.get("errors") or {}).get("routes") or [], ("errors",))
    for name, r in sorted((server.get("named_routes") or {}).items()):
        rec([r] if isinstance(r, dict) else [], (f"named:{name}",))
    return seen


def _narrow_keep(paths: list | None, match: list | None) -> list | None:
    """Paths after a matcher, for the two-set walk: [] stays [] (nothing arrives); otherwise the more informative of two
    supersets of the true intersection: the matcher's own items (_paths_after_match) when they are proven outside both
    spaces (they are path patterns, so a later strip can still be proven), else the current set when IT is proven
    outside (a subset of it is too), else the matcher's items."""
    if paths == []:
        return []
    items = _paths_after_match(paths, match)[0]
    if not _paths_may_hit_space(items):
        return items
    if paths is not None and not _paths_may_hit_space(paths):
        return paths
    return items


_KNOWN_NESTED = {"subroute": ("routes",), "reverse_proxy": ("handle_response",), "intercept": ("handle_response",)}


def _nested_route_keys(h: dict) -> list[str]:
    """Keys of a handler that hold nested routes or handlers this tool does NOT follow (wac-099 🔴-2): anything under an
    unknown handler that contains a 'handler' or 'routes' key, and the same under a known handler outside the containers
    followed (subroute.routes, reverse_proxy/intercept handle_response[].routes; e.g. a subroute's 'errors')."""
    follow = _KNOWN_NESTED.get(str(h.get("handler")), ())
    found: list[str] = []
    for key, value in h.items():
        if key == "handler" or key in follow:
            continue
        hit: list = []
        _walk_json(value, lambda node: hit.append(1) if ("handler" in node or "routes" in node) else None)
        if hit or key in ("routes", "handle_response", "errors"):
            found.append(key)
    return found


_RESPONDERS_STOP = {"reverse_proxy", "static_response", "error", "copy_response"}


def _handlers_stop(handlers: list) -> bool:
    """Does this handler list never call the next route of its list? True only when a responder that never passes on
    (reverse_proxy without handle_response, static_response, error, copy_response, file_server without pass_thru) or a subroute whose routes
    include an unconditional, ungrouped route that stops, is reached on every path. Anything else: may continue."""
    for h in handlers or []:
        kind = h.get("handler")
        # forward_auth is a reverse_proxy with handle_response: on auth success it falls through to the next
        # handler, so it does not stop (review wac-099 r2, D2/D5).
        if (kind in _RESPONDERS_STOP and not (kind == "reverse_proxy" and h.get("handle_response"))) or (
            kind == "file_server" and not h.get("pass_thru")
        ):
            return True
        if kind == "subroute":
            if any(not r.get("match") and "group" not in r and _handlers_stop(r.get("handle") or []) for r in h.get("routes") or []):
                return True
    return False


def _route_may_continue(route: dict) -> bool:
    """Can a request that matched this route reach the routes after it (with this route's path change)?"""
    return not _handlers_stop(route.get("handle") or [])


def _rewrite_forwarder_check(rep: Report, routes: list, host: str, upstream: str, skip: set[int], named: dict | None = None,
                             chain: str = "") -> int:
    """wac-097 (review wac-095 🟡-2, g18; review wac-096 🔴-1, 🟡-1): a reverse_proxy to operator-query (recognised by
    the PORT of ``upstream`` on any host spelling) that runs after a PATH CHANGE on its own chain - ``handle_path``,
    ``uri strip_prefix`` / ``replace`` / ``path_regexp`` or ``rewrite`` between the matcher and the proxy, or the
    proxy's OWN ``rewrite`` (``reverse_proxy { rewrite … }``, forward_auth's ``uri``) - forwards what the change
    produced, not what its matcher saw: ``handle_path /x/*`` sends ``GET /x/m/v1/watcher/dialogs`` to operator-query as
    /m/v1/watcher/dialogs, ``handle /foo/* { reverse_proxy <oq> { rewrite /v1/watcher/dialogs … } }`` sends every
    /foo/... as GET /v1/watcher/dialogs. Such a forwarder fails unless the changed path is proven to stay outside the
    /m/v1/watcher prefix space AND operator-query's /v1/watcher gateway space (same proofs as
    path_change_into_space); ``handle @mobile { uri strip_prefix /m … }`` on /m/v1/accounts, /m/v1/operator/orders/*
    passes (the cut patterns /v1/accounts, /v1/operator/orders/* are outside both). Containers followed: subroute
    routes, ``handle_response[].routes`` (original request), ``invoke`` -> ``named_routes`` in the caller's context;
    after a path change an unknown or recursive name, or another handler with nested routes, fails as UNCOMPARABLE.
    A matcher AFTER the change only narrows the possible paths to its own items (they are judged against both spaces;
    wac-099 🔴-1: it no longer clears the change, so handle_path /x/* { reverse_proxy /v1/* <oq> } fails).
    The watcher ports (9090/9100) are not judged here: every route to the watcher must pass the browser checks (basic
    auth, cleared X-Watcher-* and Authorization, injected placeholder; an unexercised watcher route fails), and the
    watcher serves no /m/... or /v1/... path. Returns the number of forwarders judged."""
    gw_port = dial_endpoint(upstream)[1]
    judged = 0
    # The walk carries TWO path sets (wac-099 🔴-1): U = paths that reached here unchanged, C = paths produced by a path
    # change ([] = none, None = any). A matcher narrows each set separately (_narrow_keep); a change moves U | C into C;
    # only C is judged at a reverse_proxy. So a matcher after a change never "clears" it, and a change proven outside
    # (a pre-snippet strip_suffix) does not make a later panel /v1/* forwarder look changed.

    def walk(handlers: list, U, C, trail: tuple, match, stack: tuple) -> tuple:
        nonlocal judged
        for h in handlers or []:
            kind = h.get("handler")
            if kind == "subroute":
                U, C = walk_list(h.get("routes") or [], U, C, trail, match, stack)
            elif kind == "invoke":
                name = str(h.get("name", ""))
                target = (named or {}).get(name)
                if not isinstance(target, dict) or name in stack:
                    U, C = [], None          # unknown: reported by _container_check / _forwarder_check
                else:
                    U, C = walk_list([target], U, C, trail + (f"invoke:{name}",), match, stack + (name,))
            elif kind == "rewrite":
                new, fields = _paths_after_rewrite(h, _paths_union(U, C))
                if fields:
                    U, C = [], new
            elif kind in ("reverse_proxy", "intercept"):
                # handle_response routes run with the ORIGINAL request (the proxy's own rewrite only touches its copy)
                for i, hr in enumerate(h.get("handle_response") or []):
                    aU, aC = walk_list(hr.get("routes") or [], U, C, trail + (f"handle_response{i}",), match, stack)
                    U, C = _paths_union(U, aU), _paths_union(C, aC)
                if kind == "intercept":
                    continue
                sent = C
                if isinstance(h.get("rewrite"), dict):
                    new, fields = _paths_after_rewrite(h["rewrite"], _paths_union(U, C))
                    if fields:
                        sent = new
                if sent == []:
                    continue
                judged += 1
                if not _paths_may_hit_space(sent):
                    continue
                where = f"{chain}route {'.'.join(map(str, trail))} ({_route_matcher_summary(match)})"
                for d in _dials(h) or [""]:
                    try:
                        _host_class, port = dial_endpoint(d)
                    except Uncomparable as exc:
                        rep.fail(f"UNCOMPARABLE {where} may forward a path changed into the /m/v1/watcher or /v1/watcher space: {exc}")
                        continue
                    if port == gw_port:
                        rep.fail(f"{where} may forward a path changed into the /m/v1/watcher prefix space or operator-query's "
                                 f"/v1/watcher space to {d} (port {port}) (a strip/replace/rewrite before reverse_proxy - handle_path, "
                                 "uri, the proxy's own rewrite; a matcher after the change only narrows it; not proven to stay "
                                 "outside): only the snippet may route the prefix to operator-query (§9.14.3, wac-097)")
            elif _nested_route_keys(h):
                U, C = [], None              # unknown container: reported by _container_check
        return U, C

    def walk_list(sub: list, U, C, trail: tuple, match, stack: tuple) -> tuple:
        # A route's change reaches its later siblings only when the route can hand the request on (_route_may_continue),
        # and never a later route of the SAME group (Caddy skips the rest of a group once one of its routes matched).
        contribs: list = []
        for i, r in enumerate(sub or []):
            if id(r) in skip:
                continue
            g = r.get("group")
            iU, iC = U, C
            for cg, aU, aC in contribs:
                if g is None or cg != g:
                    iU, iC = _paths_union(iU, aU), _paths_union(iC, aC)
            m = r.get("match")
            aU, aC = walk(r.get("handle") or [], _narrow_keep(iU, m), _narrow_keep(iC, m), trail + (i,), m or match, stack)
            if _route_may_continue(r):
                contribs.append((g, aU, aC))
        for _g, aU, aC in contribs:
            U, C = _paths_union(U, aU), _paths_union(C, aC)
        return U, C

    for index, route in enumerate(routes):
        if id(route) in skip or _host_excludes_route(route, host, True):
            continue
        m = route.get("match")
        walk(route.get("handle") or [], _narrow_keep(None, m), [], (index,), m, ())
    return judged


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
        # an unparsable dial (None) counts as reaching the gateway: fail-safe for the negatives
        and any(same_endpoint(d, upstream) is not False for d in _dials(out.chain[-1].handler) or [""])
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
    if not any(_is_watcher_dial(d) for d in dials):
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
            _shadow_check(rep, levels + [(site, first_group, None)], host, "prospective (routes before the first handle block)",
                          shadow_probes(lines), server.get("named_routes") or {})
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
            _shadow_check(rep, list(first.ancestry) + [(first.routes, first.index, None)], host, "snippet", shadow_probes(lines),
                          server.get("named_routes") or {})
        for line in lines:
            for method in line.methods:
                out = emu(method, line.sample())
                if out is not None:
                    _check_gateway_outcome(rep, f"{method} {line.sample()}", out, line, upstream)
    named = server.get("named_routes") or {}
    errors = (server.get("errors") or {}).get("routes") or []
    before_c = len(rep.failures)
    inspected = _container_check(rep, server)
    if len(rep.failures) == before_c:
        rep.ok(f"every nested route container is one this tool follows ({inspected} handler(s) in routes, errors, named routes)")
    _forwarder_check(rep, server.get("routes", []), list(PREFIX_PROBES), host, upstream, skip, named=named)
    # wac-097 (review wac-096 🟡-1): the error chain (handle_errors) is a second entry; nothing in it may forward the prefix
    _forwarder_check(rep, errors, list(PREFIX_PROBES), host, upstream, set(), named=named, chain="errors ")
    before = len(rep.failures)
    judged = _rewrite_forwarder_check(rep, server.get("routes", []), host, upstream, skip, named)
    judged += _rewrite_forwarder_check(rep, errors, host, upstream, set(), named, chain="errors ")
    if len(rep.failures) == before:
        rep.ok(f"no forwarder to operator-query sends a path changed into the /m/v1/watcher or /v1/watcher space "
               f"({judged} forwarder(s) after a path change judged; routes, errors, handle_response, named routes)")
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
        if not out.responded or not any(same_endpoint(d, upstream) for d in _dials(out.chain[-1].handler)):
            rep.fail(f"mobile {sample}: does not reach {upstream}")
            continue
        strips = [s for s in out.chain if s.handler.get("handler") == "rewrite"]
        touched = any(_touches(_touched_names(_header_ops(s.handler)), "authorization") for s in out.chain)
        if len(strips) != 1 or touched:
            rep.fail(f"mobile {sample}: strips={len(strips)} authorization_touched={touched}")
        else:
            rep.ok(f"mobile {sample} -> {upstream}{out.final_path} (one strip, Authorization untouched)")
    watcher_routes = [(t, r) for t, r in _walk_all_routes(server)
                      if any(h.get("handler") == "reverse_proxy" and any(_is_watcher_dial(d) for d in _dials(h))
                             for h in r.get("handle", []))]
    covered: set[tuple[int, ...]] = set()
    for sample in browser_samples:
        method, path = _parse_sample(sample)
        out = emu(method, path)
        if out is None:
            continue
        if before_deploy and out.responded and out.chain[-1].handler.get("handler") == "reverse_proxy" \
                and any(_is_watcher_dial(d) for d in _dials(out.chain[-1].handler)):
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


PROBE_GLOBAL_KEYS = ("admin", "persist_config", "auto_https", "default_bind", "http_port", "https_port")
PROBE_FAKE_AUTH = "Bearer o0-probe-not-a-token"
# never inherited by the probe's Caddy: a proxy would carry stub-bound requests elsewhere, OTEL_* would point tracing at a collector
PROBE_ENV_DROP = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "FTP_PROXY")


PROBE_HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")


def probe_caddyfile(text: str, site_address: str, site_port: int, http_port: int, https_port: int, site_host: str = "") -> str:
    """Copy of the production Caddyfile for the local probe: ONLY the site address and the global
    admin/persist_config/auto_https/default_bind/http_port/https_port options change (the line diff
    is printed). Upstreams are NOT touched here: every dial is pinned on the adapted JSON
    (pin_probe_config), whatever its spelling.
    wac-092 (review wac-090 🟡-3): the site header becomes ``http://<site_host>:<port>`` with the
    PRODUCTION host name (default: the site address), not 127.0.0.1; ``default_bind 127.0.0.1``
    keeps the listener on loopback and every probe request carries ``Host: <site_host>``, so a
    ``host`` matcher inside the site (``@h host jp-bot.*.wang``) behaves as in production."""
    site_host = site_host or site_address
    if not PROBE_HOST_RE.fullmatch(site_host):
        raise ArtifactError(f"probe host {site_host!r} is not a plain host name (pass --host <production site name>)")
    lines = text.splitlines()
    first = next((i for i, l in enumerate(lines) if l.strip() and not l.strip().startswith("#")), None)
    probe_opts = ["\tadmin off", "\tpersist_config off", "\tauto_https off", "\tdefault_bind 127.0.0.1",
                  f"\thttp_port {http_port}", f"\thttps_port {https_port}"]
    if first is not None and lines[first].strip() == "{":
        end = next(i for i in range(first + 1, len(lines)) if lines[i].strip() == "}" and not lines[i].startswith(("\t", " ")))
        if any(l.strip().split(" ")[0] in PROBE_GLOBAL_KEYS and l.rstrip().endswith("{") for l in lines[first + 1:end]):
            raise ArtifactError("global option block ('admin { ... }' etc.): edit it by hand in the copy (the probe only rewrites one-line options)")
        body = [l for l in lines[first + 1:end] if l.strip().split(" ")[0] not in PROBE_GLOBAL_KEYS]
        lines = lines[:first + 1] + probe_opts + body + lines[end:]
    else:
        lines = ["{"] + probe_opts + ["}"] + lines
    hits = 0
    for i, l in enumerate(lines):
        # wac-094 (PC-3): a site header may itself start with '{' (``{$CADDY_DOMAIN} {``); only the bare global '{' is skipped
        if l and not l[0].isspace() and l.rstrip().endswith("{") and not l.startswith(("(", "#")) and l.strip() != "{":
            head = l.rstrip()[:-1].rstrip()
            addrs = [a.strip() for a in re.split(r"[\s,]+", head) if a.strip()]
            if site_address in addrs:
                hits += 1
                lines[i] = f"http://{site_host}:{site_port} {{"
    if hits != 1:
        raise ArtifactError(f"site address {site_address!r} found in {hits} site header line(s), want exactly 1")
    return "\n".join(lines) + "\n"


def _walk_json(node, fn) -> None:
    if isinstance(node, dict):
        fn(node)
        for v in list(node.values()):
            _walk_json(v, fn)
    elif isinstance(node, list):
        for v in node:
            _walk_json(v, fn)


def pin_probe_config(adapted: dict, site_port: int, gateway_upstream: str, watcher_upstream: str,
                     stubs: dict[str, str]) -> tuple[dict, list[str]]:
    """The config the probe RUNS (review wac-088 🟡-3): only the server of the probe site, listening
    on 127.0.0.1:<site_port>; every reverse_proxy upstream anywhere in it (handle, subroutes,
    handle_response, errors, named routes) pinned to a local stub: operator-query (any loopback
    spelling of ``gateway_upstream``) -> stubs["oq"], the watcher -> stubs["watcher"], everything
    else (other services, Tailscale addresses, unix sockets, placeholders) -> stubs["sink"];
    forward proxies and active health checks removed; admin off, config not persisted; no other
    server and no other app (tls, pki, logging ...). dynamic_upstreams cannot be pinned: refused."""
    admin = adapted.get("admin") or {}
    if admin.get("disabled") is not True:
        raise ArtifactError("the adapted probe copy does not say 'admin off' (the probe never opens an admin endpoint)")
    servers = (adapted.get("apps") or {}).get("http", {}).get("servers", {}) or {}
    site = [(n, s) for n, s in servers.items() if any(str(a).endswith(f":{site_port}") for a in s.get("listen", []))]
    if len(site) != 1:
        raise ArtifactError(f"{len(site)} servers listen on the probe site port, want exactly 1")
    name, server = site[0][0], copy.deepcopy(site[0][1])
    notes = [f"servers dropped (not probed): {len(servers) - 1}",
             f"apps dropped: {','.join(sorted(k for k in adapted.get('apps', {}) if k != 'http')) or '-'}",
             f"top-level keys dropped: {','.join(sorted(k for k in adapted if k not in ('admin', 'apps'))) or '-'}"]
    server["listen"] = [f"127.0.0.1:{site_port}"]
    server["automatic_https"] = {"disable": True}
    for key in ("tls_connection_policies", "logs"):
        if server.pop(key, None) is not None:
            notes.append(f"server {key} removed")
    counts = {"oq": 0, "watcher": 0, "sink": 0}
    seen: dict[str, str] = {}

    def pin(node: dict) -> None:
        if node.get("handler") != "reverse_proxy":
            return
        if "dynamic_upstreams" in node:
            raise ArtifactError("a reverse_proxy uses dynamic_upstreams: it cannot be pinned to a stub, the probe does not run")
        ups = node.get("upstreams") or []
        if not ups:
            raise ArtifactError("a reverse_proxy has no static upstreams: it cannot be pinned to a stub")
        new = []
        for u in ups:
            dial = str(u.get("dial", ""))
            label = "oq" if same_endpoint(dial, gateway_upstream) else "watcher" if same_endpoint(dial, watcher_upstream) else "sink"
            counts[label] += 1
            seen.setdefault(dial, label)
            new.append({"dial": stubs[label]})
        node["upstreams"] = new
        if node.pop("health_checks", None) is not None:
            notes.append("reverse_proxy health_checks removed (active checks dial their own upstream)")
        transport = node.get("transport") or {}
        for key in ("forward_proxy_url", "network_proxy"):
            if transport.pop(key, None) is not None:
                notes.append(f"reverse_proxy transport {key} removed")

    _walk_json(server, pin)
    notes += [f"dial {d} -> {label}" for d, label in sorted(seen.items())]
    notes.append(f"dials pinned: oq={counts['oq']} watcher={counts['watcher']} sink={counts['sink']}")
    http = {k: v for k, v in adapted["apps"]["http"].items() if k in ("http_port", "https_port")}
    pinned = {"admin": {"disabled": True, "config": {"persist": False}}, "apps": {"http": {**http, "servers": {name: server}}}}
    return pinned, notes


def assert_probe_pinned(cfg: dict, allowed_dials: set[str], site_port: int) -> list[str]:
    """Machine check right before `caddy run`: nothing in the config may listen off loopback or dial
    anything but the three stubs. Returns the problems (empty = safe to run)."""
    problems = []
    admin = cfg.get("admin") or {}
    if admin.get("disabled") is not True or (admin.get("config") or {}).get("persist") is not False:
        problems.append("admin must be disabled and the config not persisted")
    if set(cfg) - {"admin", "apps"}:
        problems.append(f"unexpected top-level keys {sorted(set(cfg) - {'admin', 'apps'})}")
    if set(cfg.get("apps") or {}) != {"http"}:
        problems.append(f"apps must be exactly ['http'] (got {sorted(cfg.get('apps') or {})})")
    servers = ((cfg.get("apps") or {}).get("http") or {}).get("servers") or {}
    if len(servers) != 1:
        problems.append(f"{len(servers)} servers, want exactly 1")
    for s in servers.values():
        if s.get("listen") != [f"127.0.0.1:{site_port}"]:
            problems.append(f"listen {s.get('listen')} is not exactly 127.0.0.1:{site_port}")

    def check(node: dict) -> None:
        if "dial" in node and str(node["dial"]) not in allowed_dials:
            problems.append(f"dial {node['dial']} is not a local stub")
        if node.get("handler") == "reverse_proxy":
            if "dynamic_upstreams" in node or not node.get("upstreams"):
                problems.append("reverse_proxy without pinned static upstreams")
            if "health_checks" in node:
                problems.append("reverse_proxy health_checks left in")
            if set(node.get("transport") or {}) & {"forward_proxy_url", "network_proxy"}:
                problems.append("reverse_proxy transport goes through a forward proxy")

    _walk_json(cfg, check)
    return problems


class _Stub:
    def __init__(self, label: str, hits: list):
        import http.server

        class Handler(http.server.BaseHTTPRequestHandler):
            def _r(self):
                hits.append((label, self.command, self.path, self.headers.get("Authorization"), "X-Watcher-Proxy-Auth" in self.headers))
                body = f"STUB {label}".encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass    # Caddy stopped mid-request (a signal during the live checks): no traceback noise (review wac-092 💭-2)
            do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = do_OPTIONS = _r

            def log_message(self, *_a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.addr = f"127.0.0.1:{self.port}"
        # short poll interval: shutdown() returns within ~50 ms (review wac-090 🟡-4: the 0.5 s default
        # made the cleanup window long enough to be hit by a Ctrl-C)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _raw_request(port: int, method: str, target: str, headers: dict[str, str] | None = None, host: str = "127.0.0.1") -> tuple[int, bytes]:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    extra = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
    s.sendall(f"{method} {target} HTTP/1.1\r\nHost: {host}\r\n{extra}Content-Length: 0\r\nConnection: close\r\n\r\n".encode("latin1"))
    data = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), body


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _probe_env(xdg: Path, adapt_env: list[str] | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() not in PROBE_ENV_DROP and not k.upper().startswith("OTEL_")}
    # Caddy's autosave.json and data dir go to a throwaway directory, never ~/.config/caddy,
    # ~/Library/Application Support/Caddy or /var/lib/caddy (review wac-088 §5.2, G24)
    env.update(XDG_CONFIG_HOME=str(xdg / "config"), XDG_DATA_HOME=str(xdg / "data"), HOME=str(xdg))
    # review wac-090 💭-1: a `tracing` handler would export to localhost:4317 by default
    env["OTEL_SDK_DISABLED"] = "true"
    for kv in adapt_env or []:
        k, _, v = kv.partition("=")
        env[k] = v
    return env


# wac-094 (review wac-092 🟡-1): parameter values the live checks send on every parameter line (all
# matched by the line's [^/]+): real account ids, symbols in both cases, Telegram-style ids, short, long
# and dotted values, and file names with the usual image/video suffixes in both cases. The probe cannot
# enumerate the space (verify's step 1 does that); this makes the common concrete spellings visible live.
PROBE_PARAM_VALUES = (
    "account-a", "ACCOUNT-A", "account-b", "account-c", "account-d", "a", "abc", "0", "1", "42",
    "BTCUSDT", "btcusdt", "ETHUSDT", "SOLUSDT", "1000PEPEUSDT", "-1001234567890", "1234567890",
    "x.png", "x.PNG", "x.jpg", "x.JPG", "x.jpeg", "x.gif", "x.webp", "x.mp4", "x.txt", "x.json",
    "1700000000000-1.jpg", "1700000000000-1.png", "a.b.c", ".hidden", "x.", "x-y_z~1",
    "v" * 200, "seg." * 40 + "png",
)


REWRITE_ENTRY_TAILS = ("/m/v1/watcher/status", "/m/v1/watcher/dialogs", "/m/v1/watcher/trading/accounts", "/m/v1/watcher/media/x.png")
OQ_ENTRY_TAILS = ("/v1/watcher/status", "/v1/watcher/dialogs")
REWRITE_ENTRY_MAX = 128
_ENTRY_TARGET = re.compile(r"/(?:[A-Za-z0-9/._~-]|%2e)*")


def _is_wgw_route(route: dict) -> bool:
    return any((m.get("path_regexp") or {}).get("pattern", "").startswith("^" + WATCHER_PREFIX)
               or (m.get("path_regexp") or {}).get("pattern") == FALLBACK_REGEX for m in route.get("match") or [])


def rewrite_entry_targets(config: dict, listen_port: str) -> list[str]:
    """wac-097 (review wac-095 🟡-2, review wac-096 🔴-1): request targets OUTSIDE both the /m/v1/watcher space and
    operator-query's /v1/watcher space that a path change in the site would turn into a path of either, derived from
    the adapted config (snippet routes excluded; main routes, subroutes, handle_response routes, named routes reached
    by invoke and the error chain): for a strip_path_prefix P, P + each tail (REWRITE_ENTRY_TAILS and OQ_ENTRY_TAILS;
    g16, g18: /x/m/v1/watcher/..., /x/v1/watcher/...); for a uri_substring find F -> replace R, each tail with R
    replaced by F (g17: /s/status); for a constant uri (a rewrite handler or a reverse_proxy's own rewrite) inside
    either space, the route's path patterns with '*' and '?' filled (g15: /static/x; review wac-096: /foo/x for
    ``reverse_proxy { rewrite /v1/watcher/dialogs }``); for a strip_path_suffix S, both prefixes + S and each tail + S;
    for a P of k > 1 segments also P + '/%2e%2e' * i + tail (i < k: the encoded '..' that pops part of P after the
    trim). Only plain targets (letters, digits, '/._~-', '%2e') are kept, at most REWRITE_ENTRY_MAX. path_regexp
    rewrites and placeholders are left to verify's rule (path_change_into_space)."""
    out: list[str] = []
    tails = REWRITE_ENTRY_TAILS + OQ_ENTRY_TAILS

    def add(target: str) -> None:
        if len(out) < REWRITE_ENTRY_MAX and _ENTRY_TARGET.fullmatch(target) and not _in_any_space(target) and target not in out:
            out.append(target)

    def from_spec(spec: dict, pats: list[str]) -> None:
        prefix = spec.get("strip_path_prefix")
        if isinstance(prefix, str) and prefix and "{" not in prefix:
            p = prefix if prefix.startswith("/") else "/" + prefix
            nseg = len([s for s in p.split("/") if s])
            for tail in tails:
                add(p.rstrip("/") + tail)
                for i in range(1, nseg):
                    add(p.rstrip("/") + "/%2e%2e" * i + tail)
        for rep_ in spec.get("uri_substring") or []:
            find, repl = str(rep_.get("find", "")), str(rep_.get("replace", ""))
            if find and repl and "{" not in find + repl:
                for tail in tails:
                    i = tail.lower().find(repl.lower())
                    if i >= 0:
                        add(tail[:i] + find + tail[i + len(repl):])
        suffix = spec.get("strip_path_suffix")
        if isinstance(suffix, str) and suffix and "{" not in suffix:
            add(EXTERNAL_PREFIX + suffix)
            add(OQ_GATEWAY_PREFIX + suffix)
            for tail in tails:
                add(tail + suffix)
        part = _uri_path_part(str(spec.get("uri") or ""))
        lit = _constant_path(part) if part else None
        if lit and _in_any_space(lit):
            for p in pats:
                if not any(c in p for c in "[]\\{}%"):
                    filled = p.replace("*", "x").replace("?", "x")
                    add(filled if filled.startswith("/") else "/" + filled)

    try:
        server = _one_server(config, listen_port)
    except Uncomparable:
        return []
    named = server.get("named_routes") or {}

    def visit(routes: list, pats: list[str], stack: tuple) -> None:
        for route in routes or []:
            if _is_wgw_route(route):
                continue
            here = next((list(map(str, m["path"])) for m in route.get("match") or [] if m.get("path")), pats)
            for h in route.get("handle") or []:
                kind = h.get("handler")
                if kind == "rewrite":
                    from_spec(h, here)
                elif kind in ("reverse_proxy", "intercept"):
                    if kind == "reverse_proxy" and isinstance(h.get("rewrite"), dict):
                        from_spec(h["rewrite"], here)
                    for hr in h.get("handle_response") or []:
                        visit(hr.get("routes") or [], here, stack)
                elif kind == "subroute":
                    visit(h.get("routes") or [], here, stack)
                elif kind == "invoke":
                    name = str(h.get("name", ""))
                    if isinstance(named.get(name), dict) and name not in stack:
                        visit([named[name]], here, stack + (name,))

    visit(server.get("routes", []), [], ())
    visit((server.get("errors") or {}).get("routes") or [], [], ())
    return out


def double_slash_variants(path: str) -> list[str]:
    """``path`` with ONE extra '/' next to each of its slashes (review wac-090 🟡-2):
    ``/m/v1/watcher/status`` -> ``//m/v1/...``, ``/m//v1/...``, ``/m/v1//watcher/...``, ``/m/v1/watcher//status``.
    The snippet's ``path_regexp`` and ``uri strip_prefix`` clean them away; a ``path`` pattern that
    itself contains ``//`` does not, so a header rewrite written as ``/m//v1/watcher/<line>`` only
    shows up on these requests."""
    return [path[:i] + "/" + path[i:] for i, c in enumerate(path) if c == "/"]


def _probe_live_checks(port: int, hits: list, lines: list[Line], host: str = "127.0.0.1",
                       entries: list[str] | tuple = ()) -> tuple[int, list[str]]:
    """The raw-request checks against the running probe Caddy. Every forward to operator-query carries
    a fake caller Authorization that must arrive unchanged, without X-Watcher-Proxy-Auth (review
    wac-088 🟡-1: any header rewrite that shadows a table path shows up here). Every request carries
    ``Host: <host>`` (the production site name, wac-092). wac-097: every rewrite entry target
    (rewrite_entry_targets) is sent with and without the fake Authorization; whatever reaches operator-query
    must carry exactly what the caller sent and no X-Watcher-Proxy-Auth, and nothing may reach the watcher
    (the probe sends no basic credentials)."""
    fails: list[str] = []
    n = 0

    def req(method, target, headers=None):
        return _raw_request(port, method, target, headers, host)

    def expect(cond: bool, msg: str) -> None:
        nonlocal n
        n += 1
        if not cond:
            fails.append(msg)

    def forwarded(method, target, path, headers=None):
        hdrs = {"Authorization": PROBE_FAKE_AUTH} if headers is None else headers
        before = len(hits)
        status, _body = req(method, target, hdrs)
        new = hits[before:]
        ok = status == 200 and len(new) == 1 and new[0][:3] == ("oq", method, path)
        expect(ok, f"forward {method} {target!r} -> {status} hits={[h[:3] for h in new]} want oq {path!r}")
        if ok:
            expect(new[0][3] == hdrs.get("Authorization") and not new[0][4],
                   f"forward {method} {target!r}: the caller's Authorization changed or X-Watcher-Proxy-Auth injected on the way to operator-query")
        return new[0] if ok else None

    def fallback404(method, target):
        before = len(hits)
        status, body = req(method, target)
        expect(status == 404 and body == b"" and len(hits) == before, f"fallback {method} {target!r} -> {status} body={len(body)}B hits={len(hits) - before}")

    def not_forwarded(method, target):
        before = len(hits)
        status, _body = req(method, target)
        new = [h for h in hits[before:] if h[0] in ("oq", "watcher")]
        expect(not new, f"{method} {target!r} -> {status} reached operator-query or the watcher {[h[:3] for h in new]}")

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
        # wac-092 (review wac-090 🟡-2): the same line with a doubled slash at every position is cleaned
        # and forwarded with the caller's Authorization untouched
        for target in double_slash_variants(ex):
            forwarded(m0, target, ex[len(MOBILE_PREFIX):])
        # wac-094 (review wac-092 🟡-1): a header rewrite written for ONE parameter value, a suffix or a case
        # (…/accounts/account-a, media/*.png, */risks/btcusdt) never sees the 'x' fill: every parameter line is
        # sent again with each PROBE_PARAM_VALUES entry, with every method of the line
        if line.param_count():
            for value in PROBE_PARAM_VALUES:
                target = line.sample(value)
                for m in line.methods:
                    forwarded(m, target, target[len(MOBILE_PREFIX):])
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
    # no caller Authorization: nothing may be injected on the way to operator-query
    forwarded("GET", "/m/v1/watcher/status", "/v1/watcher/status", {})
    forwarded("GET", "/m/v1/watcher/trading/accounts", "/v1/watcher/trading/accounts", {})
    forwarded("GET", "/m//v1/watcher/trading/accounts", "/v1/watcher/trading/accounts", {})
    before = len(hits)
    _status, _b = req("GET", "/m/v1/accounts", {"Authorization": PROBE_FAKE_AUTH})
    mob = hits[before:]
    expect(len(mob) == 1 and mob[0][:3] == ("oq", "GET", "/v1/accounts") and mob[0][3] == PROBE_FAKE_AUTH and not mob[0][4],
           f"/m/v1/accounts: want oq /v1/accounts with the caller's Authorization (got {[h[:3] for h in mob]})")
    for target in ("/watcher/", "/api/status", "/media/1700000000000-1.jpg"):
        before = len(hits)
        status, _b = req("GET", target)
        expect(status == 401 and len(hits) == before, f"browser {target} without credentials -> {status}, stub hits {len(hits) - before}")
    # wac-097 (review wac-095 🟡-2): entries that a path change in the site turns into prefix-space paths (g16-g18)
    for target in entries:
        for hdrs in ({"Authorization": PROBE_FAKE_AUTH}, {}):
            before = len(hits)
            status, _b = req("GET", target, hdrs)
            new = hits[before:]
            wrong = [h for h in new if h[0] == "watcher" or (h[0] == "oq" and (h[3] != hdrs.get("Authorization") or h[4]))]
            expect(not wrong, f"rewrite entry GET {_redact_path(target)!r} ({'with' if hdrs else 'without'} Authorization) -> {status}: "
                              f"reached {[(h[0], _redact_path(h[2])) for h in wrong]} with a changed Authorization, X-Watcher-Proxy-Auth, "
                              "or the watcher without basic credentials")
    return n, fails


PROBE_TMP_PREFIX = "o0-caddy-probe-xdg-"
OWNER_FILE = "o0-probe-owner.json"
OWNER_FORMAT = "o0-caddy-probe-owner.v1"
PROBE_SCRIPT_MARK = "o0_caddy_watcher_routes"


def _process_table() -> list[tuple[int, int, str]] | None:
    """(pid, pgid, command) of every process, or None when ``ps`` is unavailable."""
    try:
        out = subprocess.run(["ps", "-A", "-ww", "-o", "pid=,pgid=,command="], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    table = []
    for row in out.stdout.splitlines():
        parts = row.strip().split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            table.append((int(parts[0]), int(parts[1]), parts[2] if len(parts) > 2 else ""))
    return table


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def scan_probe_residue(tmpdir: Path, out=print) -> dict[str, int]:
    """wac-094 (review wac-092 🟡-2): report and clean what an earlier probe left behind after a SIGKILL.
    Only what the probe's OWN owner record proves is touched:
      * ``<tmpdir>/o0-caddy-probe-xdg-*`` with a valid OWNER_FILE whose probe pid is no longer a running
        probe: every process whose command line names that directory (the ``caddy run --config
        <dir>/probe-run.json`` group), plus recorded child pids whose command line names the recorded
        ``.o0probe``, is stopped (SIGTERM to its group when the group leader is a recorded pid, SIGKILL after
        5 s); the ``.o0probe`` is deleted only when its sha256 equals the recorded one; then the directory
        is removed -> PROBE_RESIDUE_CLEANED;
      * such a directory whose probe is still running -> PROBE_RESIDUE_BUSY, untouched;
      * a directory without a valid owner record (older tool version, another tool) -> PROBE_RESIDUE_FOREIGN,
        untouched; a process that names o0-caddy-probe-xdg- but belongs to none of the cleaned or busy
        directories -> PROBE_RESIDUE_ORPHAN pid=<n>, untouched (pgrep -fl o0-caddy-probe-xdg).
    Only directory base names and pids are printed (no paths, no content)."""
    counts = {"cleaned": 0, "busy": 0, "foreign": 0, "orphans": 0, "unproven": 0}
    table = _process_table()
    own_pid = os.getpid()
    claimed: set[int] = set()
    for d in sorted(tmpdir.glob(PROBE_TMP_PREFIX + "*")):
        name = d.name
        record = None
        if d.is_dir() and not d.is_symlink():
            try:
                record = json.loads((d / OWNER_FILE).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                record = None
        if not (isinstance(record, dict) and record.get("format") == OWNER_FORMAT and isinstance(record.get("probe_pid"), int)
                and isinstance(record.get("pids"), list) and all(isinstance(p, int) for p in record["pids"])
                and isinstance(record.get("probe_file"), str)):
            counts["foreign"] += 1
            out(f"PROBE_RESIDUE_FOREIGN dir={name}: no owner record of this tool (older version or another tool); NOT touched: "
                "check it and remove it yourself (it may hold a bcrypt hash)")
            continue
        dpath = str(d) + os.sep      # mkdtemp suffixes have one length; the separator rules out any prefix clash
        mine = [(pid, pgid) for pid, pgid, cmd in table or [] if dpath in cmd and pid != own_pid]
        probe_pid = record["probe_pid"]
        if probe_pid != own_pid and any(pid == probe_pid and PROBE_SCRIPT_MARK in cmd for pid, _g, cmd in table or []):
            counts["busy"] += 1
            claimed |= {pid for pid, _g in mine}
            out(f"PROBE_RESIDUE_BUSY dir={name}: its probe (pid {probe_pid}) is still running; NOT touched")
            continue
        recorded = set(record["pids"])
        probe_file = Path(record["probe_file"])
        mine += [(pid, pgid) for pid, pgid, cmd in table or [] if pid in recorded and str(probe_file) in cmd and pid != own_pid]
        if table is None and any(_pid_alive(p) for p in recorded):
            counts["unproven"] += 1
            out(f"PROBE_RESIDUE_UNPROVEN dir={name}: a recorded child pid is alive and the process table is unavailable; NOT touched")
            continue
        mine = list(dict.fromkeys(mine))
        claimed |= {pid for pid, _g in mine}
        for pid, pgid in mine:
            try:
                if pgid in recorded and pgid == pid:
                    os.killpg(pgid, signal.SIGTERM)
                else:
                    os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.monotonic() + 5
        while any(_pid_alive(pid) for pid, _g in mine) and time.monotonic() < deadline:
            time.sleep(0.05)
        for pid, pgid in mine:
            try:
                if pgid in recorded and pgid == pid:
                    os.killpg(pgid, signal.SIGKILL)
                elif _pid_alive(pid):
                    os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        o0probe = "absent"
        if probe_file.name.endswith(".o0probe") and probe_file.is_file() and not probe_file.is_symlink():
            if record.get("probe_file_sha256") and _sha256_file(probe_file) == record["probe_file_sha256"]:
                probe_file.unlink()
                o0probe = "removed"
            else:
                o0probe = "kept(content not the recorded one)"
        shutil.rmtree(d, ignore_errors=True)
        counts["cleaned"] += 1
        out(f"PROBE_RESIDUE_CLEANED dir={name} processes_stopped={len(mine)} o0probe={o0probe} dir_removed={not d.exists()}")
    for pid, _g, cmd in table or []:
        if PROBE_TMP_PREFIX in cmd and pid not in claimed and pid != own_pid and "ps -A" not in cmd:
            counts["orphans"] += 1
            out(f"PROBE_RESIDUE_ORPHAN pid={pid}: its command line names {PROBE_TMP_PREFIX}* but no owner record proves it is this "
                "tool's; NOT touched (pgrep -fl o0-caddy-probe-xdg)")
    out("PROBE_RESIDUE_SCAN " + " ".join(f"{k}={v}" for k, v in counts.items()) + (" process_table=unavailable" if table is None else ""))
    return counts


PROBE_SIGNALS = tuple(getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGHUP") if hasattr(signal, name))


class _ProbeInterrupted(BaseException):
    """SIGINT/SIGTERM/SIGHUP during the probe. A BaseException, so no ``except Exception`` (or
    ``except OSError`` in the connect loop) swallows it."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


class _ProbeRun:
    """Everything the probe must remove or stop, whatever ends it (review wac-090 🟡-4): the
    ``.o0probe`` copy and the temporary state dir (the pinned run JSON) both hold the basic-auth
    bcrypt hash; the ``caddy run`` process group; a running ``caddy version``/``adapt`` child; the stubs.

    * First SIGINT/SIGTERM/SIGHUP: ``_ProbeInterrupted`` is raised in the main flow; cmd_probe's
      ``finally`` sets ``cleaning`` as its FIRST statement and calls cleanup().
    * A signal inside a critical section (creating the temp dir, starting a child and registering
      it) is held until the section ends, so nothing exists that cleanup() does not know about.
    * Any signal while ``cleaning`` is set (a second signal, or the first one landing in the
      ``finally``) runs emergency(): unlink ``.o0probe``, SIGKILL the child process groups, wait at
      most 2 s, remove the temp dir, exit 128+signal. It never waits for the stubs.
    cleanup() removes ``.o0probe`` FIRST (the running Caddy never reads it), then stops Caddy
    (SIGTERM to its process group, SIGKILL after 10 s), then removes the temp dir (Caddy can no
    longer write into it), and only then closes the stubs.
    Not covered: SIGKILL of the probe itself, power loss. Then ``<copy>.o0probe`` (bcrypt hash), the
    temporary dir ``$TMPDIR/o0-caddy-probe-xdg-*`` (``probe-run.json`` with the hash) AND a running Caddy
    (its own session, it does not die with the probe) are left behind (review wac-092 🟡-2). wac-094: every
    temporary dir carries an owner record (OWNER_FILE: the probe's pid, the ``.o0probe`` path and the sha256
    of what was written there, the pids of every child it started); the next probe run scans ``$TMPDIR``
    first (scan_probe_residue) and cleans what that record proves to be its own; the runbook keeps
    ``ls -A``, ``ls -d $TMPDIR/o0-caddy-probe-xdg-*`` and ``pgrep -fl o0-caddy-probe-xdg`` afterwards."""

    def __init__(self, probe_file: Path, keep: bool) -> None:
        self.probe_file = probe_file
        self.keep = keep
        self.xdg: Path | None = None
        self.procs: list[subprocess.Popen] = []
        self.stubs: list[_Stub] = []
        self.cleaning = False
        self.defer = 0
        self.pending: int | None = None
        self.old: dict = {}
        self.owner: dict = {}

    def install(self) -> None:
        for s in PROBE_SIGNALS:
            self.old[s] = signal.signal(s, self._on_signal)

    def restore(self) -> None:
        for s, handler in self.old.items():
            signal.signal(s, handler)
        self.old = {}

    def _on_signal(self, signum, _frame) -> None:
        if self.cleaning:
            self.emergency(signum)
        if self.defer:
            self.pending = signum
            return
        raise _ProbeInterrupted(signum)

    def critical(self):
        run = self

        class _Critical:
            def __enter__(self):
                run.defer += 1

            def __exit__(self, exc_type, _exc, _tb):
                run.defer -= 1
                if not run.defer and run.pending is not None and exc_type is None:
                    signum, run.pending = run.pending, None
                    raise _ProbeInterrupted(signum)
                return False

        return _Critical()

    def mkdtemp(self) -> Path:
        with self.critical():
            self.xdg = Path(tempfile.mkdtemp(prefix=PROBE_TMP_PREFIX))
            self.owner = {"format": OWNER_FORMAT, "probe_pid": os.getpid(), "probe_file": str(self.probe_file),
                          "probe_file_sha256": None, "pids": []}
            self._write_owner()
        return self.xdg

    def _write_owner(self) -> None:
        """The owner record (wac-094): written before the thing it describes exists, replaced atomically."""
        if self.xdg is None:
            return
        tmp = self.xdg / (OWNER_FILE + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(self.owner, handle)
        os.replace(tmp, self.xdg / OWNER_FILE)

    def write_probe_file(self, text: str) -> None:
        """Record the sha256 of the .o0probe text FIRST, then write it (0600): a later scan removes the file
        only when its content is exactly what this run recorded."""
        with self.critical():
            self.owner["probe_file_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
            self._write_owner()
        fd = os.open(self.probe_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)

    def add_stub(self, label: str, hits: list) -> _Stub:
        with self.critical():
            stub = _Stub(label, hits)
            self.stubs.append(stub)
        return stub

    def spawn(self, cmd: list[str], **kw) -> subprocess.Popen:
        """Popen in a NEW process group (a wrapper script and the Caddy it starts are stopped
        together), registered before any signal can be handled."""
        with self.critical():
            proc = subprocess.Popen(cmd, start_new_session=True, **kw)
            self.procs.append(proc)
            if self.owner:
                self.owner["pids"].append(proc.pid)
                self._write_owner()
        return proc

    def run_capture(self, cmd: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
        proc = self.spawn(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out, err = proc.communicate()
        self.procs.remove(proc)
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)

    @staticmethod
    def _killpg(proc: subprocess.Popen, sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _remove_probe_file(self) -> None:
        if not self.keep:
            try:
                self.probe_file.unlink()
            except FileNotFoundError:
                pass

    def cleanup(self) -> None:
        self.cleaning = True
        self._remove_probe_file()
        for proc in list(self.procs):
            if proc.poll() is None:
                self._killpg(proc, signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._killpg(proc, signal.SIGKILL)
                    proc.wait()
            self._killpg(proc, signal.SIGKILL)      # anything the leader left behind in its group
        self.procs = []
        if self.xdg is not None:
            shutil.rmtree(self.xdg, ignore_errors=True)
        for stub in self.stubs:
            stub.close()
        self.stubs = []

    def emergency(self, signum: int) -> None:
        self._remove_probe_file()
        for proc in self.procs:
            self._killpg(proc, signal.SIGKILL)
        deadline = time.monotonic() + 2
        while any(p.poll() is None for p in self.procs) and time.monotonic() < deadline:
            time.sleep(0.02)
        if self.xdg is not None:
            shutil.rmtree(self.xdg, ignore_errors=True)
        name = signal.Signals(signum).name
        # os.write, not print: the main flow may be inside a print when the signal lands
        os.write(1, f"CADDY_PROBE_INTERRUPTED signal={name} during cleanup: .o0probe and the temporary dir removed, caddy killed\n".encode())
        os._exit(128 + signum)


def cmd_probe(args: argparse.Namespace) -> int:
    if Path("/srv/trader-v3").exists():
        print("CADDY_PROBE_REFUSED this host has /srv/trader-v3 (jp-24): the probe is a LOCAL, non-production command")
        return 1
    try:
        lines, meta = load_artifacts(args.paths, args.snippet)
    except ArtifactError as exc:
        print(f"CADDY_PROBE_FAILED artifacts: {exc}")
        return 1
    copy_path = args.caddyfile.resolve()
    staged = copy_path.parent / SNIPPET_FILE
    if not copy_path.is_file() or not staged.is_file() or staged.read_bytes() != (args.snippet or args.paths.parent / SNIPPET_FILE).read_bytes():
        print(f"CADDY_PROBE_FAILED {copy_path.name} must exist and {SNIPPET_FILE} next to it must be a byte-identical copy of the committed snippet (the import is relative)")
        return 1
    host = args.host or args.site_address
    if not PROBE_HOST_RE.fullmatch(host):
        print(f"CADDY_PROBE_FAILED --host {host!r} is not a plain host name (pass the production site name)")
        return 1
    probe_file = copy_path.with_name(copy_path.name + ".o0probe")
    run = _ProbeRun(probe_file, args.keep)
    # signals first (review wac-092 💭-3): a Ctrl-C from here on ends in CADDY_PROBE_INTERRUPTED, not a traceback
    run.install()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from o0_tool import redact_line
    # wac-094 (review wac-092 🟡-2): what an earlier probe left behind when it was SIGKILLed
    scan_probe_residue(Path(tempfile.gettempdir()))
    if probe_file.exists() or probe_file.is_symlink():
        run.restore()
        print(f"CADDY_PROBE_FAILED {probe_file.name} already exists next to the copy (an earlier probe was killed or ran with --keep; "
              "it holds the bcrypt hash): read it if needed, delete it, then rerun")
        return 1
    cand_sha, snip_sha = _sha256_file(copy_path), _sha256_file(staged)
    ident = f"candidate_sha256={cand_sha} snippet_sha256={snip_sha}"
    print(f"PROBE_INPUT {ident}  (stage C C-1: --probe-candidate-sha256 {cand_sha} --probe-snippet-sha256 {snip_sha})")
    hits: list = []
    fails: list[str] = []
    n, version, verify_passes = 0, "?", 0
    interrupted: int | None = None
    try:
        # everything that writes (the .o0probe copy holds the bcrypt hash, the temp dir the pinned JSON)
        # or starts a process is inside this try and registered with `run` first (review wac-090 🟡-4)
        xdg = run.mkdtemp()
        oq, wa, sink = run.add_stub("oq", hits), run.add_stub("watcher", hits), run.add_stub("sink", hits)
        port, hp, hsp = _free_port(), _free_port(), _free_port()
        original = copy_path.read_text(encoding="utf-8")
        text = probe_caddyfile(original, args.site_address, port, hp, hsp, host)
        run.write_probe_file(text)
        diff = [redact_line(d) for d in difflib.unified_diff(original.splitlines(), text.splitlines(), "production-copy", "probe", n=0, lineterm="")]
        print("---- probe copy vs production copy (line diff, redacted)")
        print("\n".join(diff))
        env = _probe_env(xdg, args.adapt_env)
        version = (run.run_capture([args.caddy, "version"], env).stdout.strip().split(" ") or ["?"])[0]
        print(f"caddy version {version}")
        adapted = run.run_capture([args.caddy, "adapt", "--adapter", "caddyfile", "--config", str(probe_file)], env)
        if adapted.returncode != 0:
            print(f"CADDY_PROBE_FAILED adapt rc={adapted.returncode} stderr_lines={len(adapted.stderr.splitlines())} {ident}")
            return 1
        config = json.loads(adapted.stdout)
        # verify the copy as written (production upstream addresses and site name), exactly like stage C preflight
        rep = run_verify(config, lines, host=host, listen_port=str(port), upstream=args.upstream,
                         mobile_samples=DEFAULT_MOBILE_SAMPLES, browser_samples=DEFAULT_BROWSER_SAMPLES)
        verify_passes = rep.passes
        fails = [f"verify: {f}" for f in rep.failures]
        pinned, notes = pin_probe_config(config, port, args.upstream, args.watcher_upstream,
                                         {"oq": oq.addr, "watcher": wa.addr, "sink": sink.addr})
        problems = assert_probe_pinned(pinned, {oq.addr, wa.addr, sink.addr}, port)
        for note in notes:
            print(f"PROBE_PIN {redact_line(note)}")
        if problems:
            for p in problems:
                print(f"FAIL pin: {redact_line(p)}")
            print(f"CADDY_PROBE_FAILED the pinned config is not local-only; caddy was NOT started {ident}")
            return 1
        print(f"PROBE_LOCAL_ONLY listen=127.0.0.1:{port} host={host} admin=off persist=off servers=1 apps=http dials=stubs_only")
        run_file = xdg / "probe-run.json"
        fd = os.open(run_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(pinned, handle)
        err_file = xdg / "caddy-run.err"
        with open(err_file, "wb") as err:
            proc = run.spawn([args.caddy, "run", "--config", str(run_file)], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err)
        for _ in range(150):
            if proc.poll() is not None:
                break
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        if proc.poll() is not None:
            last = (err_file.read_text(encoding="utf-8", errors="replace").strip().splitlines() or [""])[-1]
            print(f"CADDY_PROBE_FAILED caddy run exited rc={proc.returncode}: {redact_line(last)[:300]} {ident}")
            return 1
        entries = rewrite_entry_targets(config, str(port))
        print(f"PROBE_REWRITE_ENTRIES targets={len(entries)} (path changes outside the snippet; each sent with and without Authorization)")
        n, live_fails = _probe_live_checks(port, hits, lines, host, entries)
        fails += live_fails
    except ArtifactError as exc:
        print(f"CADDY_PROBE_FAILED {exc} {ident}")
        return 1
    except _ProbeInterrupted as exc:
        interrupted = exc.signum
    finally:
        run.cleaning = True     # FIRST: from here on any signal takes the emergency path (never aborts the cleanup)
        run.cleanup()
        run.restore()
        if args.keep and probe_file.exists():
            print(f"NOTE --keep: {probe_file.name} kept; it contains the basic-auth bcrypt hash: delete it yourself after reading")
    if interrupted is not None:
        print(f"CADDY_PROBE_INTERRUPTED signal={signal.Signals(interrupted).name}: .o0probe and the temporary dir removed, caddy stopped {ident}")
        return 128 + interrupted
    counts = {label: sum(1 for h in hits if h[0] == label) for label in ("oq", "watcher", "sink")}
    stub_hits = f"stub_hits=oq:{counts['oq']},watcher:{counts['watcher']},sink:{counts['sink']}"
    # verify and live failures are capped SEPARATELY: an UNCOMPARABLE matcher makes verify fail on every emulated
    # request, which must not hide what the running Caddy did (wac-092)
    verify_fails = [f for f in fails if f.startswith("verify: ")]
    live_only = [f for f in fails if not f.startswith("verify: ")]
    for group in (verify_fails, live_only):
        for f in group[:40]:
            print(f"FAIL {f}")
        if len(group) > 40:
            print(f"NOTE {len(group) - 40} more {'verify' if group is verify_fails else 'live'} failure(s) not printed")
    if fails:
        print(f"CADDY_PROBE_FAILED caddy={version} checks={n} verify_passes={verify_passes} failures={len(fails)} "
              f"verify_failures={len(verify_fails)} live_failures={len(live_only)} {stub_hits} {ident}")
        return 1
    print(f"CADDY_PROBE_OK caddy={version} live_checks={n} verify_passes={verify_passes} lines={len(lines)} host={host} "
          f"yaml_sha256={meta['_yaml_sha256']} phase_max={meta['_phase_max']} dot_and_double_slash=cleaned_and_forwarded {stub_hits} {ident}")
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
    # 1b. Caddy v2.10.2 MatchPath pins (wac-090; the port was compared with Go path.Match and Caddy's MatchPath)
    for pattern, path, want in (
            ("/m/v1/w?tcher/status", "/m/v1/watcher/status", True), ("/m/v1/[w]atcher/dialogs", "/M/V1/WATCHER/dialogs", True),
            ("/m/v1/[^x]atcher", "/m/v1/watcher", True), ("/m/v1/[a-z]atcher/*/x", "/m/v1/watcher/a/x", True),
            ("/m/v1/watch\\er/status", "/m/v1/watcher/status", True), ("/M/V1/Watcher/*", "/m/v1/watcher/status", True),
            ("/m/*/watcher", "/m/v1/watcher", True), ("/m/*/watcher", "/m/v1/x/watcher", False),
            ("/m/v1/w?tcher*", "/m/v1/watcher/x", False),       # one trailing '*': Caddy's fast prefix match, '?' literal
            ("*w?tcher*", "/m/v1/watcher", False),               # '*...*': fast substring match, '?' literal
            ("/m/v1/watcher/[^s]*", "/m/v1/watcher/status", False), ("/m/v1/w?tcher", "/m/v1/w/tcher", False)):
        assert _caddy_path_match(pattern, path) is want, ("MatchPath pin", pattern, path, want)
        checks += 1
    for pattern in ("/m/v1/{http.request.uri.query.zz}watcher/status", "/m/v1/%77atcher/status", "/m/v1/[watcher", "/m/v1/watcher\\"):
        try:
            _caddy_path_match(pattern, "/m/v1/watcher/status")
            raise AssertionError(("must be uncomparable", pattern))
        except Uncomparable:
            pass
        assert _path_may_hit(pattern, "/m/v1/watcher/status"), ("uncomparable must count as a hit", pattern)
        checks += 1
    for dial, want in (("127.0.0.1:8183", ("loopback", "8183")), ("localhost:8183", ("loopback", "8183")), ("[::1]:8183", ("loopback", "8183")),
                       ("tcp/127.0.0.1:8183", ("loopback", "8183")), (":8183", ("loopback", "8183")), ("0.0.0.0:8183", ("loopback", "8183")),
                       ("127.0.0.2:8183", ("loopback", "8183")), ("[::ffff:127.0.0.1]:8183", ("loopback", "8183")),
                       ("LOCALHOST:9090", ("loopback", "9090")), ("100.89.58.40:8183", ("100.89.58.40", "8183"))):
        assert dial_endpoint(dial) == want, ("dial", dial, dial_endpoint(dial))
        checks += 1
    for dial in ("unix//run/oq.sock", "{env.OQ}:8183", "127.0.0.1:8000-8010", "127.0.0.1", "udp/127.0.0.1:8183", ""):
        try:
            dial_endpoint(dial)
            raise AssertionError(("dial must be uncomparable", dial))
        except Uncomparable:
            pass
        checks += 1
    assert _pattern_touches_prefix("/m/v1/watcher/extra") and _pattern_touches_prefix("/M/V1/WATCHER*") and \
        _pattern_touches_prefix("/m/v1/w?tcher/extra") and not _pattern_touches_prefix("/m/v1/other") and not _pattern_touches_prefix("*.js")
    checks += 1

    def must_uncomparable(fn, *a):
        try:
            fn(*a)
        except Uncomparable:
            return
        raise AssertionError(("must be uncomparable", fn.__name__, a))

    # 1b'. wac-094 (review wac-092 🟡-1): step 1 over the whole /m/v1/watcher prefix space, not over samples.
    # v15..v20 of the review (a concrete parameter value, suffix and middle globs, an unanchored regexp) hit;
    # patterns whose literal part rules the space out do not.
    for pattern in ("/m/v1/watcher/trading/accounts/account-a", "/m/v1/watcher/media/*.png", "*/risks/btcusdt",
                    "/m/v1/watcher/trading/accounts/a*", "/m/v1/watcher/trading/channels/[0-9]*", "*.png", "*.PNG", "*watcher*",
                    "?m/v1/x", "[/]m/v1/watcher", "/M/V1/WATCHER/X", "/m/*", "/*", "*", "/m/v1/watcher", "/m/v1/watcher/*/x",
                    "/m/v1/*/status", "/m/v1/watch*", "/m/v1/watcher%2fstatus", "/m/v1/{http.vars.x}", "/m//v1/x", "/m/v1/watcİer"):
        assert _path_pattern_may_hit_space(pattern), ("path pattern must hit the prefix space", pattern)
        checks += 1
    for pattern in ("/static/?ld", "/old/*", "/m/v1/w?tcher*", "/m/v1/watcherx*", "/m/v1/other", "/v1/*", "/m/v1/accounts",
                    "/m/v1/operator/orders/*", "/watcher/*", "/api/*", "/media/*", "/healthz", "/m/v1/watcherx/*", "/m/v2/*"):
        assert not _path_pattern_may_hit_space(pattern), ("path pattern cannot hit the prefix space", pattern)
        checks += 1
    for pattern in ("account-a$", "%2[fF]", "^.*watcher", "^/(m|x)/", "^/api|watcher", "(?i)^/M/", "^(?i)/api", "^/M",
                    "^/m/v1/watcher/trading/accounts/account-a$", "^/mx?", "^\\/m\\/v1", "^", "^/m/v1/watcher\\n", "^[/]m", "^/m/v1/watcher$"):
        assert _regexp_may_hit_space(pattern), ("path_regexp must hit the prefix space", pattern)
        checks += 1
    for pattern in ("^/static/", "^/m/v1$", "^/api/(a|b)", "^\\/api\\/", "^/m/v1/watcherx", "^/v1/watcher/", "^/static/.*\\.js$"):
        assert not _regexp_may_hit_space(pattern), ("path_regexp cannot hit the prefix space", pattern)
        checks += 1
    # wac-097 (review wac-095 🟡-3): RE2 character classes whose first member is ']' ([]…], [^]…]), POSIX classes and
    # \Q…\E no longer hide a top-level '|' (g01c, g04c, g04d; the review's black-box shapes); '|' inside a class, an
    # escaped '|' and an unterminated \Q (all literal) are not alternations
    for pattern in ("^/static[](]|account-a$", "^/static[^](]|account-a$", "^/static[](]|\\.png$", "^/static[](]|a\\.bmp$",
                    "^/api[](][]|]|/", "^/api[^](][^]]|/status$", "^/api[](]\\(|/status$", "^/static\\Q(\\E|a\\.bmp$",
                    "^/static[[:alpha:](]|a\\.bmp$", "^/api[^](][[:alpha:]]|\\.png$", "^/x[\\]]|a$", "^/x[(]|a$", "^/x(a|b))|c$"):
        assert _re_top_level_alternation(pattern) and _regexp_may_hit_space(pattern), ("top-level '|' must be seen", pattern)
        checks += 1
    for pattern in ("^/static\\Q(|x", "^/static[]|]x", "^/static[^]|]x", "^/static[[:alpha:]|]x", "^/static\\|x", "^/static[(|]x",
                    "^/static(a|b)x", "^/static\\Q|\\Ex"):
        assert not _re_top_level_alternation(pattern) and not _regexp_may_hit_space(pattern), ("no top-level '|'", pattern)
        checks += 1
    assert _re_class_end("[]a]b", 0) == 4 and _re_class_end("[^]a]b", 0) == 5 and _re_class_end("[[:alpha:]]x", 0) == 11 \
        and _re_class_end("[a", 0) is None and _re_top_level_alternation("^/x[a|b")   # unclosed class: counted as a hit
    checks += 1
    # wac-097 (review wac-095 🟡-2): path changes. Whatever the matcher, a change counts as "may enter the prefix
    # space" unless proven outside (constant clean path outside; strip_prefix cut patterns outside; strip_suffix on
    # paths that cannot start with /m/v1/watcher)
    rw = lambda match, **h: {"match": match, "handle": [{"handler": "rewrite", **h}]}  # noqa: E731
    for name, route in {
            "g16 strip_prefix /x on /x/*": rw([{"path": ["/x/*"]}], strip_path_prefix="/x"),
            "g17 uri_substring": rw([{"path": ["/s/*"]}], uri_substring=[{"find": "/s/", "replace": "/m/v1/watcher/", "limit": 1}]),
            "path_regexp rewrite": rw([{"path": ["/x/*"]}], path_regexp=[{"find": "^/x", "replace": ""}]),
            "try_files placeholder uri": rw([{"path": ["/x/*"]}], uri="{http.matchers.file.relative}"),
            "g15 constant uri inside the space": rw([{"path": ["/static/*"]}], uri="/m/v1/watcher/status"),
            "constant uri, upper case": rw([{"path": ["/static/*"]}], uri="/M/V1/WATCHER?x=1"),
            "constant uri with a dot segment (cleaned later)": rw([{"path": ["/static/*"]}], uri="/m/v1/./watcher/status"),
            "constant uri with '%'": rw([{"path": ["/static/*"]}], uri="/m/v1/%77atcher/status"),
            "relative uri": rw([{"path": ["/static/*"]}], uri="m/v1/watcher/status"),
            "strip_suffix x on /m/v1/watcherx*": rw([{"path": ["/m/v1/watcherx*"]}], strip_path_suffix="x"),
            "strip_suffix on a path_regexp that may start with the prefix": rw([{"path_regexp": {"pattern": "^/m/v1/watch"}}], strip_path_suffix="x"),
            "strip_suffix '.' only": rw([{"path": ["/api/*"]}], strip_path_suffix=".."),
            "strip_prefix on ^/static/ (regexp paths unknown)": rw([{"path_regexp": {"pattern": "^/static/"}}], strip_path_prefix="/static"),
            "strip_prefix /api on /a* (head is a start of the prefix)": rw([{"path": ["/a*"]}], strip_path_prefix="/api"),
            "strip_prefix without matcher": rw(None, strip_path_prefix="/x"),
            "strip_prefix on *.png": rw([{"path": ["*.png"]}], strip_path_prefix="/x"),
            "strip_prefix /m on /m/m/*": rw([{"path": ["/m/m/*"]}], strip_path_prefix="/m"),
            # wac-097 real-Caddy differential: /m/v1/%2e%2e/m/v1/watcher/status is matched as /m/m/v1/watcher/status and
            # leaves the strip as /m/v1/watcher/status (the encoded '..' pops only the /v1 of the prefix)
            "strip_prefix /m/v1 on /m/m/* (a segment-prefix of P cut)": rw([{"path": ["/m/m/*"]}], strip_path_prefix="/m/v1"),
            "strip_prefix /x/y/z on /x/m/*": rw([{"path": ["/x/m/*"]}], strip_path_prefix="/x/y/z"),
            "strip_prefix with a dot segment": rw([{"path": ["/api/*"]}], strip_path_prefix="/api/.."),
            "strip_prefix leaving a dot-led remainder": rw([{"path": ["/m...x"]}], strip_path_prefix="/m"),
            "strip_prefix with a placeholder": rw([{"path": ["/x/*"]}], strip_path_prefix="/{http.vars.p}"),
            # review wac-096 second round 🔴-A: an empty path part changes nothing and proves nothing
            "query-only uri (path part empty)": rw([{"path": ["/old/*"]}], uri="?v=1"),
            "fragment-only uri '#frag'": rw([{"path": ["/old/*"]}], uri="#frag"),
            "uri /v1/auth then uri_substring auth -> watcher/dialogs in one object": rw([{"path": ["/foo/*"]}], uri="/v1/auth",
                                                                                     uri_substring=[{"find": "auth", "replace": "watcher/dialogs"}]),
            "constant uri into operator-query's /v1/watcher": rw([{"path": ["/foo/*"]}], uri="/V1/Watcher/dialogs"),
            "uri then strip_prefix into the space": rw([{"path": ["/x/*"]}], uri="/x/m/v1/watcher/status", strip_path_prefix="/x"),
            "reverse_proxy rewrite strip": {"match": [{"path": ["/q/*"]}], "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": "127.0.0.1:8183"}],
                                                                                   "rewrite": {"strip_path_prefix": "/q"}}]},
            "nested subroute strip": {"match": [{"path": ["/x/*"]}], "handle": [{"handler": "subroute", "routes": [{"handle": [
                {"handler": "rewrite", "strip_path_prefix": "/x"}]}]}]},
            # review wac-096 second round 🔴-B: handle_response routes see the ORIGINAL request, not the proxy's rewritten copy
            "strip inside handle_response of a proxy that rewrites its copy to /ping": {"match": [{"path": ["/x/*"]}], "handle": [
                {"handler": "reverse_proxy", "rewrite": {"uri": "/ping"}, "upstreams": [{"dial": "127.0.0.1:7001"}],
                 "handle_response": [{"routes": [{"handle": [{"handler": "rewrite", "strip_path_prefix": "/x"}]}]}]}]},
            "invoke of a named route that strips (named routes passed)": {"match": [{"path": ["/x/*"]}], "handle": [{"handler": "invoke", "name": "strip"}]},
            "invoke of an unknown named route": {"match": [{"path": ["/x/*"]}], "handle": [{"handler": "invoke", "name": "nope"}]},
            "unknown handler with nested routes": {"match": [{"path": ["/x/*"]}], "handle": [{"handler": "o0_plugin", "routes": []}]},
            # wac-099 🔴-2 C1: intercept's handle_response routes run with the original request and are followed
            "C1 intercept handle_response rewrite into /v1/watcher": {"match": [{"path": ["/x/*"]}], "handle": [{"handler": "subroute", "routes": [
                {"handle": [{"handler": "intercept", "handle_response": [{"routes": [{"group": "g", "handle": [{"handler": "rewrite",
                    "uri": "/v1/watcher/dialogs"}]}, {"handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": "127.0.0.1:8183"}]}]}]}]},
                            {"handler": "static_response", "status_code": 404}]}]}]},
            "subroute with its own errors routes": {"match": [{"path": ["/x/*"]}], "handle": [{"handler": "subroute", "routes": [],
                                                                                           "errors": {"routes": []}}]},
            # wac-099 🟡-1 (M12): an exact-literal path_regexp that starts with a prefix, then strip_suffix x
            "E2 CONNECT ^/m/v1/watcherx$ + strip_suffix x": rw([{"method": ["CONNECT"], "path_regexp": {"pattern": "^/m/v1/watcherx$"}}], strip_path_suffix="x"),
            "CONNECT ^/v1/watcherx$ + strip_suffix x (operator-query space)": rw([{"method": ["CONNECT"], "path_regexp": {"pattern": "^/v1/watcherx$"}}],
                                                                                   strip_path_suffix="x"),
    }.items():
        assert path_change_into_space(route, {"strip": {"handle": [{"handler": "rewrite", "strip_path_prefix": "/x"}]}}), \
            ("path change must count as entering the prefix space", name)
        checks += 1
    for name, route in {
            "p03 rewrite /healthz /api/status": rw([{"path": ["/healthz"]}], uri="/api/status"),
            "method-only rewrite": rw([{"path": ["/old/*"]}], method="GET"),
            "p05 regexp rewritten to a constant": rw([{"path_regexp": {"pattern": "^/static/.*\\.(js|css)$"}}], uri="/static/bundle.js"),
            "p06 strip_suffix / on /api/*": rw([{"path": ["/api/*"]}], strip_path_suffix="/"),
            "strip_suffix .html on /docs/*": rw([{"path": ["/docs/*"]}], strip_path_suffix=".html"),
            "strip_suffix on an anchored regexp elsewhere": rw([{"path_regexp": {"pattern": "^/static/"}}], strip_path_suffix="/"),
            "strip_prefix /api on /api/v2/*": rw([{"path": ["/api/v2/*"]}], strip_path_prefix="/api"),
            "strip_prefix /m on the mobile paths": rw([{"path": ["/m/v1/accounts", "/m/v1/operator/orders/*"]}], strip_path_prefix="/m"),
            "strip_prefix /reports on /reports/daily/*": rw([{"path": ["/reports/daily/*"]}], strip_path_prefix="reports"),
            "strip_prefix /api on /static/* (never starts with it)": rw([{"path": ["/static/*"]}], strip_path_prefix="/api"),
            "strip_prefix /api/v2 on /api/v2/x/* (every cut outside)": rw([{"path": ["/api/v2/x/*"]}], strip_path_prefix="/api/v2"),
            "no path change at all": {"match": [{"path": ["/x/*"]}], "handle": [{"handler": "headers", "request": {"set": {"X": ["1"]}}}]},
    }.items():
        assert path_change_into_space(route) is None, ("path change proven outside the prefix space", name, path_change_into_space(route))
        checks += 1
    assert _paths_after_rewrite({"strip_path_prefix": "/m"}, [("path", "/m/v1/operator/orders/*")])[0] == \
        [("path", "/m/v1/operator/orders/*"), ("path", "/v1/operator/orders/*")], "strip keeps the uncut pattern (encoded '..' pops) and adds the cut one"
    assert _constant_path("/api/status") == "/api/status" and _uri_path_part("/a?b#c") == "/a" and _uri_path_part("?x") == "" \
        and _constant_path("/a/../b") is None and _constant_path("/a//b") is None and _constant_path("/a b") is None
    checks += 1
    assert _mset_may_hit_space({"method": ["PUT"], "path": ["/m/v1/watcher/trading/accounts/a*"]}, "jp-bot.balen.wang", False)
    assert not _mset_may_hit_space({"method": ["PUT"], "path": ["/old/*"]}, "jp-bot.balen.wang", False)
    assert not _mset_may_hit_space({"host": ["other.example"], "path": ["*.png"]}, "jp-bot.balen.wang", True)
    assert _mset_may_hit_space({"host": ["other.example"], "path": ["*.png"]}, "jp-bot.balen.wang", False)
    assert _may_hit_space(None, "jp-bot.balen.wang") and _may_hit_space([{"not": [{"path": ["/x"]}]}], "jp-bot.balen.wang")
    checks += 1

    # 1c. wac-092 (review wac-090 🟡-1): non-ASCII. The reviewer's 12 dangerous black-box cases (tool "no hit",
    # real Caddy v2.10.2 "hit"; all U+0130, which Go lower-cases to 'i') plus other shapes: UNCOMPARABLE = hit
    review_091_dangerous = (
        ("/m/v1/watcher/dİalogs", "/M/V1/WATCHER/dialogs"),
        ("/m/v1/watcher/tradİng/accounts", "/M/V1/WATCHER/TRADING/ACCOUNTS"),
        ("/m/v1/watcher/tradİng/accounts", "/m/v1/watcher/trading/accounts"),
        ("/M/V1/Wat[a-z]her/Dİa[l]ogs", "/M/V1/WATCHER/dialogs"),
        ("/m/v1/watcher/dİalogs", "/M/V1/WATCHER/dialogs"),
        ("/m/v1/watcher/media[İ]/1700000000000-1.jpg", "/M/V1/WATCHER/MEDIAİ/1700000000000-1.JPG"),
        ("/m/v1/watcher/media[İ]/1700000000000-1.jpg", "/m/v1/watcher/mediaİ/1700000000000-1.jpg"),
        ("/m/v1/watcher/tradİng/accounts", "/m/v1/watcher/trading/accounts"),
        ("/m/v1/watcher/tradİng/accounts", "/M/V1/WATCHER/TRADING/ACCOUNTS"),
        ("/M/V1/WATCHER/DI[İ^z]LOGS", "/M/V1/WATCHER/DIİLOGS"),
        ("/m/v1/watcher/dİalogs", "/M/V1/WATCHER/dialogs"),
        ("/m/v1/watcher/tradİng/accounts", "/m/v1/watcher/trading/accounts"),
    )
    for pattern, path in review_091_dangerous + (("/m/v1/watcher/trading/risKs", "/m/v1/watcher/trading/risks"),   # Kelvin sign
                                                 ("/m/v1/watcher/ſtatus", "/m/v1/watcher/status"),                 # long s
                                                 ("/m/v1/watcher/*", "/m/v1/watcher/stİtus")):                     # 💭-2: path side
        must_uncomparable(_caddy_path_match, pattern, path)
        assert _path_may_hit(pattern, path), ("non-ASCII must count as a hit", pattern, path)
        checks += 1
    assert _literal_head("/m/v1/watcİer/x") == "/m/v1/watc" and _pattern_touches_prefix("/m/v1/watcİer/x"), "non-ASCII ends the literal head"
    # 🟡-2: '//' in a path pattern (Caddy keeps the request's doubled slashes for it; the gateway's path_regexp cleans them)
    for pattern in ("/m//v1/watcher/trading/accounts", "/m/v1//watcher/status", "//m/v1/watcher/*", "/m/v1/watcher//*"):
        must_uncomparable(_caddy_path_match, pattern, "/m/v1/watcher/status")
        assert _path_may_hit(pattern, "/m/v1/watcher/status")
        checks += 1
    assert double_slash_variants("/m/v1/watcher/status") == ["//m/v1/watcher/status", "/m//v1/watcher/status",
                                                            "/m/v1//watcher/status", "/m/v1/watcher//status"], "probe '//' variants"
    # 🟡-3: Caddy MatchHost (label-wise '*', literal partial labels), placeholders and non-ASCII names UNCOMPARABLE
    for hosts, want in ((["jp-bot.*.wang"], True), (["*.balen.wang"], True), (["*.wang"], False), (["JP-BOT.Balen.Wang"], True),
                        (["jp-*.balen.wang"], False), (["*.*.*"], True), (["*.*"], False), (["other.example"], False),
                        (["other.example", "*.balen.wang"], True)):
        assert _host_match(hosts, "jp-bot.balen.wang") is want, ("MatchHost pin", hosts, want)
        checks += 1
    for hosts in (["{http.request.host}"], ["jp-bot.{env.O0_DOMAIN}"], ["jp-böt.balen.wang"]):
        must_uncomparable(_host_match, hosts, "jp-bot.balen.wang")
        assert _mset_may_hit({"host": hosts}, "/m/v1/watcher", "jp-bot.balen.wang", host_exact=True), ("uncomparable host = hit", hosts)
        checks += 1
    # F-13 step 1: at the server's top level the host is compared; inside a site it never excludes
    assert not _mset_may_hit({"host": ["other.example"]}, "/m/v1/watcher", "jp-bot.balen.wang", host_exact=True)
    assert _mset_may_hit({"host": ["other.example"]}, "/m/v1/watcher", "jp-bot.balen.wang", host_exact=False)
    checks += 1

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
        # the same regex tamper in BOTH files (a consistent snippet): only the "regex derived from the template" rule is left
        bad_re = row.split(" ")[1].replace("[^/]+$", ".+$")    # no '*': the star rule must not be what catches it
        assert art(good_list.replace(row, row.replace(row.split(" ")[1], bad_re)), good_snip.replace(row.split(" ")[1], bad_re)) is not None, \
            "regex not derived from the template, snippet consistent"
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
            "extra nested import besides the top-level one": cf_good.replace("\t\treverse_proxy 127.0.0.1:8183\n\t}\n}\n", "\t\treverse_proxy 127.0.0.1:8183\n\t}\n\thandle /x/* {\n\t\timport watcher_gateway_routes\n\t}\n}\n"),
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

    def strip_proxy_route(match, group="group21", dial="127.0.0.1:8183"):
        r = {"match": match, "handle": [{"handler": "subroute", "routes": [{"handle": [
            {"handler": "rewrite", "strip_path_prefix": "/m"}, {"handler": "reverse_proxy", "upstreams": [{"dial": dial}]}]}]}]}
        if group:
            r["group"] = group
        return r

    def inject_observer():
        return {"handler": "headers", "request": {"set": {"Authorization": ["Bearer {env.SYSTEM_OBSERVER_TOKEN}"]}}}

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

    def inject_rp() -> dict:
        return {"handler": "reverse_proxy", "headers": {"request": {"set": {"Authorization": ["Bearer {env.SYSTEM_OBSERVER_TOKEN}"]}}},
                "upstreams": [{"dial": "127.0.0.1:8183"}]}
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
        "fallback 404 with a body (contract: empty body)": lambda s: s[fb(s)]["handle"][0]["routes"][0]["handle"][0].update({"body": "gone"}),
        "fallback 404 preceded by another handler": lambda s: s[fb(s)]["handle"][0]["routes"][0]["handle"].insert(0, {"handler": "headers", "response": {"set": {"X-A": ["1"]}}}),
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
        # isolated F-13 cases: the emulated requests are unaffected, ONLY the shadow check can catch them
        "no-op rewrite (strip_suffix) hits the prefix": lambda s: s.insert(0, {"match": [{"method": ["CONNECT"], "path": ["/m/v1/watcher/*"]}], "handle": [
            {"handler": "rewrite", "strip_path_suffix": "/never-there"}]}),
        "whitelisted handler, terminal, method-only matcher": lambda s: s.insert(0, {"match": [{"method": ["CONNECT"]}], "handle": [
            {"handler": "encode", "encodings": {"gzip": {}}}], "terminal": True}),
        "whitelisted handler in a handle group before the snippet": lambda s: s.insert(first_wgw(s), {"group": "group21", "match": [{"method": ["CONNECT"]}],
            "handle": [{"handler": "vars", "x": "1"}]}),
        "unevaluable path_regexp counts as a hit": lambda s: s.insert(0, {"match": [{"method": ["CONNECT"], "path_regexp": {"name": "u", "pattern": "^/m/\\pL+"}}],
            "handle": [{"handler": "rewrite", "strip_path_suffix": "/never-there"}]}),
        "import wrapped in route { } (after the handle blocks)": wrap_in_route,
        "import wrapped in route { } without a catch-all handle": wrap_in_route_no_catchall,
        "snippet inside handle_path /m* (extra strip, nested)": lambda s: s.insert(first_wgw(s), {"group": "group21", "match": [{"path": ["/m*"]}], "handle": [
            {"handler": "rewrite", "strip_path_prefix": "/m"}, {"handler": "subroute", "routes": [s.pop(first_wgw(s))]}]}),
        "well-formed unlisted line inside the block": lambda s: s.insert(first_wgw(s) + 1, {"group": "group21", "match": [{"method": ["GET"], "path_regexp": {
            "name": "wgw_r_price_alerts", "pattern": "^/m/v1/watcher/price-alerts$"}}], "handle": copy.deepcopy(s[first_wgw(s)]["handle"])}),
        "last line and fallback moved into a nested subroute": lambda s: s.insert(fb(s) - 1, {"handle": [{"handler": "subroute", "routes": [s.pop(fb(s) - 1), s.pop(fb(s))]}]}),
        # the moved line keeps its index (16 non-matching pads before it): contiguity and fallback position both hold,
        # only the "one list" rule can refuse it
        "last line nested at the same index (padded)": lambda s: s.insert(fb(s) - 1, {"handle": [{"handler": "subroute", "routes":
            [{"match": [{"path": [f"/o0-pad/{i}"]}], "handle": [{"handler": "vars", "p": str(i)}]} for i in range(fb(s) - 1)] + [s.pop(fb(s) - 1)]}]}),
        # forwarders of the prefix (d) and emulation (e)
        "forwarder of the prefix hidden behind the fallback": lambda s: s.insert(fb(s) + 1, strip_proxy_route([{"path": ["/m/v1/watcher/*"]}])),
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
        # wac-090 (review wac-088 🟡-1): path matchers with Caddy glob semantics and placeholders
        "glob '?' rewrite hits a table path": lambda s: s.insert(0, {"match": [{"path": ["/m/v1/w?tcher/status"]}], "handle": [
            {"handler": "rewrite", "uri": "/index.html"}]}),
        "glob class [w] rewrite hits the upper-case probe": lambda s: s.insert(0, {"match": [{"path": ["/m/v1/[w]atcher/dialogs"]}], "handle": [
            {"handler": "rewrite", "uri": "/index.html"}]}),
        "placeholder in a path matcher (uncomparable = hit)": lambda s: s.insert(0, {"match": [{"path": ["/m/v1/{http.request.uri.query.zz}watcher/status"]}],
            "handle": [{"handler": "rewrite", "uri": "/index.html"}]}),
        "request_header via glob injects the observer token (wac-088 §2.3)": lambda s: s.insert(0, {"match": [{"path": ["/m/v1/w?tcher/trading/accounts"]}],
            "handle": [{"handler": "headers", "request": {"set": {"Authorization": ["Bearer {env.SYSTEM_OBSERVER_TOKEN}"]}}}]}),
        # isolated: CONNECT-only, so emulation is unaffected; only case-insensitive path matching makes it hit (G10)
        "mixed-case path matcher /M/V1/Watcher/* (case-insensitive)": lambda s: s.insert(0, {"match": [{"method": ["CONNECT"], "path": ["/M/V1/Watcher/*"]}],
            "handle": [{"handler": "rewrite", "strip_path_suffix": "/never-there"}]}),
        # isolated: behind the fallback, answers without proxying: only "touches the prefix but is neither a line nor the fallback" (G13)
        "(?i) extra route after the fallback": lambda s: s.insert(fb(s) + 1, {"group": "group21", "match": [{"path_regexp": {
            "name": "wx", "pattern": "(?i)^/m/v1/watcher/extra$"}}], "handle": [{"handler": "static_response", "status_code": 204}]}),
        # isolated: a correct browser route for /healthz that ALSO lists /m/v1/watcher/* (dead behind the fallback): only the
        # watcher-port rule of the forwarder check can refuse it (G17)
        "watcher-port forwarder of the prefix hidden in a browser route": lambda s: s.insert(fb(s) + 1, {"group": "group21",
            "match": [{"path": ["/m/v1/watcher/*", "/healthz"]}], "handle": copy.deepcopy(s[-2]["handle"])}),
        # isolated: basic auth on the mobile path inside the upstream snippet (G18)
        "basic auth in the upstream snippet": lambda s: s[gw_index(s, "watcher/status")]["handle"][0]["routes"][0]["handle"].insert(0,
            {"handler": "authentication", "providers": {"http_basic": {"accounts": [{"password": "HASH", "username": "u"}]}}}),
        # review wac-088 🟡-2: operator-query by port on every spelling, unparsable dials, dead forwarders
        "localhost:8183 forwarder of /m/* after the fallback": lambda s: s.insert(fb(s) + 1, strip_proxy_route([{"path": ["/m/*"]}], dial="localhost:8183")),
        "[::1]:8183 forwarder of /m/* after the fallback": lambda s: s.insert(fb(s) + 1, strip_proxy_route([{"path": ["/m/*"]}], dial="[::1]:8183")),
        "unix-socket forwarder of /m/* (uncomparable)": lambda s: s.insert(fb(s) + 1, strip_proxy_route([{"path": ["/m/*"]}], dial="unix//run/oq.sock")),
        "dead /m/v1/watcher/extra forwarder behind the fallback": lambda s: s.insert(fb(s) + 1, strip_proxy_route([{"path": ["/m/v1/watcher/extra"]}])),
        # isolated: CONNECT-only (emulation unaffected) and the pattern hits ONE table path, none of F-13's three probes:
        # only the list-line shadow probes (shadow_probes) reach step 2
        "CONNECT rewrite on a single table path": lambda s: s.insert(0, {"match": [{"method": ["CONNECT"], "path": ["/m/v1/watcher/trading/accounts"]}],
            "handle": [{"handler": "rewrite", "strip_path_suffix": "/never-there"}]}),
        # isolated: dead behind the fallback (no negative reaches it), operator-query spelled differently / unparsable:
        # only the port rule and the UNCOMPARABLE dial rule of the forwarder check see them
        "dead /m/v1/watcher/extra forwarder spelled localhost:8183": lambda s: s.insert(fb(s) + 1, strip_proxy_route([{"path": ["/m/v1/watcher/extra"]}],
            dial="localhost:8183")),
        "dead /m/v1/watcher/extra forwarder to a unix socket (uncomparable)": lambda s: s.insert(fb(s) + 1, strip_proxy_route([{"path": ["/m/v1/watcher/extra"]}],
            dial="unix//run/oq.sock")),
        "dead dynamic_upstreams forwarder behind the fallback (uncomparable)": lambda s: s.insert(fb(s) + 1, {"group": "group21",
            "match": [{"path": ["/m/v1/watcher/extra"]}], "handle": [{"handler": "reverse_proxy", "dynamic_upstreams": {"source": "srv", "name": "oq"}}]}),
        # wac-092 (review wac-090 🟡-1): non-ASCII path patterns
        "request_header on /m/v1/watcher/tradİng/accounts injects the observer token (review n07)": lambda s: s.insert(0, {
            "match": [{"path": ["/m/v1/watcher/tradİng/accounts"]}], "handle": [inject_observer()]}),
        # isolated: CONNECT-only (emulation never evaluates the path), Kelvin sign: only "non-ASCII = hit" reaches step 2
        "CONNECT rewrite on /m/v1/watcher/trading/risKs (Kelvin sign)": lambda s: s.insert(0, {"match": [{"method": ["CONNECT"],
            "path": ["/m/v1/watcher/trading/risKs"]}], "handle": [{"handler": "rewrite", "strip_path_suffix": "/never-there"}]}),
        # 🟡-2: '//' in a path pattern
        "request_header on /m//v1/watcher/trading/accounts injects the observer token (review n08)": lambda s: s.insert(0, {
            "match": [{"path": ["/m//v1/watcher/trading/accounts"]}], "handle": [inject_observer()]}),
        "CONNECT rewrite on /m/v1//watcher/status (only the '//' rule)": lambda s: s.insert(0, {"match": [{"method": ["CONNECT"],
            "path": ["/m/v1//watcher/status"]}], "handle": [{"handler": "rewrite", "strip_path_suffix": "/never-there"}]}),
        "'//' forwarder of the prefix to operator-query after the fallback": lambda s: s.insert(fb(s) + 1, strip_proxy_route([{"path": ["/m//v1/watcher/*"]}])),
        # 🟡-3: host matchers inside the site
        "host jp-bot.*.wang + request_header injection (review n17b)": lambda s: s.insert(0, {"match": [{"host": ["jp-bot.*.wang"]}],
            "handle": [inject_observer()]}),
        "host {http.request.host} + request_header injection (review n18)": lambda s: s.insert(0, {"match": [{"host": ["{http.request.host}"]}],
            "handle": [inject_observer()]}),
        # isolated: another name, CONNECT-only; the emulation (Host jp-bot.balen.wang) never enters it, only "a host matcher
        # inside the site never excludes" sends it to step 2
        "CONNECT rewrite behind a host matcher for another name": lambda s: s.insert(0, {"match": [{"host": ["alias.balen.wang"], "method": ["CONNECT"],
            "path": ["/m/v1/watcher/*"]}], "handle": [{"handler": "rewrite", "strip_path_suffix": "/never-there"}]}),
        # isolated: a /m/* forwarder to operator-query for another name after the fallback: only the forwarder check's host rule
        "operator-query forwarder of /m/* behind a host matcher for another name": lambda s: s.insert(fb(s) + 1, strip_proxy_route(
            [{"host": ["alias.balen.wang"], "path": ["/m/*"]}])),
        # wac-094 (review wac-092 🟡-1, v15..v20): matchers that miss every sample path but select other parameter values,
        # suffixes or cases of table paths; each injects the observer token
        "v15 request_header on one account id (…/accounts/account-a)": lambda s: s.insert(0, {
            "match": [{"path": ["/m/v1/watcher/trading/accounts/account-a"]}], "handle": [inject_observer()]}),
        "v16 request_header on media/*.png": lambda s: s.insert(0, {"match": [{"path": ["/m/v1/watcher/media/*.png"]}], "handle": [inject_observer()]}),
        "v17 request_header on an unanchored path_regexp account-a$": lambda s: s.insert(0, {
            "match": [{"path_regexp": {"name": "x", "pattern": "account-a$"}}], "handle": [inject_observer()]}),
        "v18 request_header on */risks/btcusdt": lambda s: s.insert(0, {"match": [{"path": ["*/risks/btcusdt"]}], "handle": [inject_observer()]}),
        "v19 request_header on PUT …/accounts/a*": lambda s: s.insert(0, {
            "match": [{"method": ["PUT"], "path": ["/m/v1/watcher/trading/accounts/a*"]}], "handle": [inject_observer()]}),
        "v14 request_header on path_regexp %2[fF]": lambda s: s.insert(0, {
            "match": [{"path_regexp": {"name": "x", "pattern": "%2[fF]"}}], "handle": [inject_observer()]}),
        "v20 rewrite on …/channels/[0-9]* (literal prefix; a channel id may start with it)": lambda s: s.insert(0, {
            "match": [{"path": ["/m/v1/watcher/trading/channels/[0-9]*"]}], "handle": [{"handler": "rewrite", "uri": "/index.html"}]}),
        # isolated: dead forwarder of *.png to operator-query after the fallback (only the forwarder check's space rule)
        "dead *.png forwarder to operator-query after the fallback": lambda s: s.insert(fb(s) + 1, strip_proxy_route([{"path": ["*.png"]}])),
        # wac-097 (review wac-095 🟡-2): path changes that bring a path from outside the prefix into it (g15..g18)
        "g16 strip_prefix /x + request_header on /x/* (one route)": lambda s: s.insert(0, {"match": [{"path": ["/x/*"]}], "handle": [
            {"handler": "rewrite", "strip_path_prefix": "/x"}, inject_observer()]}),
        "g17 uri replace /s/ -> /m/v1/watcher/ + request_header on /s/*": lambda s: s.insert(0, {"match": [{"path": ["/s/*"]}], "handle": [
            {"handler": "rewrite", "uri_substring": [{"find": "/s/", "limit": 1, "replace": "/m/v1/watcher/"}]}, inject_observer()]}),
        "g15 rewrite /static/* to /m/v1/watcher/status (group)": lambda s: s.insert(0, {"group": "group22", "match": [{"path": ["/static/*"]}],
            "handle": [{"handler": "rewrite", "uri": "/m/v1/watcher/status"}]}),
        "g18 handle_path /x/* to operator-query with the observer token": lambda s: s.insert(fb(s) + 1, {"group": "group21", "match": [{"path": ["/x/*"]}],
            "handle": [{"handler": "subroute", "routes": [{"handle": [{"handler": "rewrite", "strip_path_prefix": "/x"}, {"handler": "reverse_proxy",
                "upstreams": [{"dial": "127.0.0.1:8183"}], "headers": {"request": {"set": {"Authorization": ["Bearer {env.SYSTEM_OBSERVER_TOKEN}"]}}}}]}]}]}),
        # isolated (wac-099 rework): without the panel /v1/* forwarder, a pre-snippet path change can only feed the snippet, so only
        # step 1's path-change rule (not the two-set forwarder walk) sees it
        "g16 without the panel: CONNECT strip_prefix /x + request_header on /x/*": lambda s: (
            s.pop(next(i for i, r in enumerate(s) if (r.get("match") or [{}])[0].get("path") == ["/v1/*"])),
            s.insert(0, {"match": [{"method": ["CONNECT"], "path": ["/x/*"]}], "handle": [{"handler": "rewrite", "strip_path_prefix": "/x"},
                                                                                        inject_observer()]})),
        # isolated: behind the fallback, nothing injected, no probe or sample reaches /x/*: only the path-change forwarder rule
        "handle_path /x/* to operator-query, nothing injected": lambda s: s.insert(fb(s) + 1, {"group": "group21", "match": [{"path": ["/x/*"]}],
            "handle": [{"handler": "subroute", "routes": [{"handle": [{"handler": "rewrite", "strip_path_prefix": "/x"},
                                                                     {"handler": "reverse_proxy", "upstreams": [{"dial": "127.0.0.1:8183"}]}]}]}]}),
        "reverse_proxy's own rewrite (strip /q) to localhost:8183": lambda s: s.insert(fb(s) + 1, {"group": "group21", "match": [{"path": ["/q/*"]}],
            "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": "localhost:8183"}], "rewrite": {"strip_path_prefix": "/q"}}]}),
        # isolated: CONNECT-only (emulation unaffected), matcher outside the space: only the path-change rule of step 1
        "CONNECT strip_prefix /x on /x/* before the snippet": lambda s: s.insert(0, {"match": [{"method": ["CONNECT"], "path": ["/x/*"]}],
            "handle": [{"handler": "rewrite", "strip_path_prefix": "/x"}]}),
        "CONNECT path_regexp rewrite on /x/*": lambda s: s.insert(0, {"match": [{"method": ["CONNECT"], "path": ["/x/*"]}],
            "handle": [{"handler": "rewrite", "path_regexp": [{"find": "^/x", "replace": ""}]}]}),
        "CONNECT try_files-style placeholder uri on /x/*": lambda s: s.insert(0, {"match": [{"method": ["CONNECT"], "path": ["/x/*"]}],
            "handle": [{"handler": "rewrite", "uri": "{http.matchers.file.relative}"}]}),
        "CONNECT strip_suffix x on /m/v1/watcherx* (/m/v1/watcherx -> /m/v1/watcher)": lambda s: s.insert(0, {"match": [{"method": ["CONNECT"],
            "path": ["/m/v1/watcherx*"]}], "handle": [{"handler": "rewrite", "strip_path_suffix": "x"}]}),
        # was benign before wac-097: GET /static/m/v1/watcher/status leaves this route as /m/v1/watcher/status
        "strip_prefix /static on ^/static/ before the snippet": lambda s: s.insert(0, {"match": [{"path_regexp": {"name": "st", "pattern": "^/static/"}}],
            "handle": [{"handler": "rewrite", "strip_path_prefix": "/static"}]}),
        # wac-097 (review wac-095 🟡-3): a class whose first member is ']' hid the top-level '|'
        "g01c request_header on ^/static[](]|account-a$": lambda s: s.insert(0, {"match": [{"path_regexp": {"name": "x", "pattern": "^/static[](]|account-a$"}}],
            "handle": [inject_observer()]}),
        "g04c request_header on ^/static[](]|\\.png$": lambda s: s.insert(0, {"match": [{"path_regexp": {"name": "x", "pattern": "^/static[](]|\\.png$"}}],
            "handle": [inject_observer()]}),
        "g04d request_header on ^/static[^](]|account-a$": lambda s: s.insert(0, {"match": [{"path_regexp": {"name": "x", "pattern": "^/static[^](]|account-a$"}}],
            "handle": [inject_observer()]}),
        # no probe parameter value ends in .bmp: only verify's parse can refuse it
        "request_header on ^/static[](]|a\\.bmp$": lambda s: s.insert(0, {"match": [{"path_regexp": {"name": "x", "pattern": "^/static[](]|a\\.bmp$"}}],
            "handle": [inject_observer()]}),
        "CONNECT rewrite on ^/api[^](][^]]|/status$": lambda s: s.insert(0, {"match": [{"method": ["CONNECT"], "path_regexp": {"name": "x",
            "pattern": "^/api[^](][^]]|/status$"}}], "handle": [{"handler": "rewrite", "strip_path_suffix": "/never-there"}]}),
        # wac-097 (review wac-096 🔴-1): a reverse_proxy's OWN rewrite (reverse_proxy { rewrite … }, forward_auth's uri)
        "review wac-096: handle /foo/* { reverse_proxy <oq> { rewrite /v1/watcher/dialogs; header_up observer } }": lambda s: s.insert(fb(s) + 1, {
            "group": "group21", "match": [{"path": ["/foo/*"]}], "handle": [{"handler": "subroute", "routes": [{"handle": [{"handler": "reverse_proxy",
                "headers": {"request": {"set": {"Authorization": ["Bearer {env.SYSTEM_OBSERVER_TOKEN}"]}}}, "rewrite": {"uri": "/v1/watcher/dialogs"},
                "upstreams": [{"dial": "127.0.0.1:8183"}]}]}]}]}),
        "reverse_proxy <oq> { rewrite /v1/watcher{path} } on /foo/* (placeholder)": lambda s: s.insert(fb(s) + 1, {"group": "group21",
            "match": [{"path": ["/foo/*"]}], "handle": [{"handler": "reverse_proxy", "rewrite": {"uri": "/v1/watcher{http.request.uri.path}"},
                                                         "upstreams": [{"dial": "127.0.0.1:8183"}]}]}),
        "forward_auth /bar/* <oq> { uri /v1/watcher/status } before the snippet": lambda s: s.insert(0, {"match": [{"path": ["/bar/*"]}], "handle": [
            {"handler": "reverse_proxy", "rewrite": {"method": "GET", "uri": "/v1/watcher/status"}, "upstreams": [{"dial": "127.0.0.1:8183"}],
             "handle_response": [{"match": {"status_code": [2]}, "routes": [{"handle": [{"handler": "vars"}]}]}]}]}),
        # wac-097 (review wac-096 🟡-1): handle_response routes and unknown containers are followed
        "sink reverse_proxy on /m/* whose handle_response forwards to <oq>": lambda s: s.insert(fb(s) + 1, {"group": "group21", "match": [{"path": ["/m/*"]}],
            "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": "127.0.0.1:7000"}], "handle_response": [{"match": {"status_code": [5]},
                "routes": [{"handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": "127.0.0.1:8183"}]}]}]}]}]}),
        "unknown handler with nested routes on /m/* (uncomparable)": lambda s: s.insert(fb(s) + 1, {"group": "group21", "match": [{"path": ["/m/*"]}],
            "handle": [{"handler": "o0_plugin", "routes": [{"handle": [{"handler": "static_response", "status_code": 204}]}]}]}),
        # isolated: dead behind the fallback (no emulated request reaches it, no path change): only the forwarder check's
        # UNCOMPARABLE for a container it does not enumerate
        "dead /m/v1/watcher/extra with an unknown nested-route container (uncomparable)": lambda s: s.insert(fb(s) + 1, {"group": "group21",
            "match": [{"path": ["/m/v1/watcher/extra"]}], "handle": [{"handler": "o0_plugin", "routes": [{"handle": [
                {"handler": "reverse_proxy", "upstreams": [{"dial": "127.0.0.1:8183"}]}]}]}]}),
        # review wac-096 second round (q1, q4, 🔴-A, 🔴-B; coordinator's second addition to wac-097)
        "q1 handle_path /x/* { reverse_proxy <oq> { rewrite ?a=1 } }": lambda s: s.insert(fb(s) + 1, {"group": "group21", "match": [{"path": ["/x/*"]}],
            "handle": [{"handler": "subroute", "routes": [{"handle": [{"handler": "rewrite", "strip_path_prefix": "/x"}]}, {"handle": [
                {"handler": "reverse_proxy", "rewrite": {"uri": "?a=1"}, "upstreams": [{"dial": "127.0.0.1:8183"}]}]}]}]}),
        "q4 handle_path /x/* { reverse_proxy <oq> { rewrite #frag } }": lambda s: s.insert(fb(s) + 1, {"group": "group21", "match": [{"path": ["/x/*"]}],
            "handle": [{"handler": "subroute", "routes": [{"handle": [{"handler": "rewrite", "strip_path_prefix": "/x"}]}, {"handle": [
                {"handler": "reverse_proxy", "rewrite": {"uri": "#frag"}, "upstreams": [{"dial": "127.0.0.1:8183"}]}]}]}]}),
        # isolated: /foo/* cannot hit, no other change: only "an empty path part proves nothing"
        "handle /foo/* { reverse_proxy <oq> { rewrite ?a=1 } } (empty path part)": lambda s: s.insert(fb(s) + 1, {"group": "group21",
            "match": [{"path": ["/foo/*"]}], "handle": [{"handler": "reverse_proxy", "rewrite": {"uri": "?a=1"}, "upstreams": [{"dial": "127.0.0.1:8183"}]}]}),
        # isolated: handle_response routes get the ORIGINAL request (after the strip, before the proxy's own rewrite /ping)
        "handle_path /x/* { reverse_proxy sink { rewrite /ping; handle_response { reverse_proxy <oq> } } }": lambda s: s.insert(fb(s) + 1, {
            "group": "group21", "match": [{"path": ["/x/*"]}], "handle": [{"handler": "subroute", "routes": [
                {"handle": [{"handler": "rewrite", "strip_path_prefix": "/x"}]},
                {"handle": [{"handler": "reverse_proxy", "rewrite": {"uri": "/ping"}, "upstreams": [{"dial": "127.0.0.1:7001"}],
                             "handle_response": [{"routes": [{"handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": "127.0.0.1:8183"}]}]}]}]}]}]}]}),
        # wac-099 🔴-1 (review wac-099 A1, A6, A7, A8): a matcher AFTER the path change (reverse_proxy /v1/*, handle /v1/*)
        "A1 handle_path /x/* { reverse_proxy /v1/* <oq> +observer }": lambda s: s.insert(fb(s) + 1, {"group": "group21", "match": [{"path": ["/x/*"]}],
            "handle": [{"handler": "subroute", "routes": [{"handle": [{"handler": "rewrite", "strip_path_prefix": "/x"}]},
                                                         {"match": [{"path": ["/v1/*"]}], "handle": [inject_rp()]}]}]}),
        "A6 handle_path /x/* { handle /v1/* { reverse_proxy <oq> +observer } }": lambda s: s.insert(fb(s) + 1, {"group": "group21",
            "match": [{"path": ["/x/*"]}], "handle": [{"handler": "subroute", "routes": [{"handle": [{"handler": "rewrite", "strip_path_prefix": "/x"}]},
                {"group": "group1", "match": [{"path": ["/v1/*"]}], "handle": [{"handler": "subroute", "routes": [{"handle": [inject_rp()]}]}]}]}]}),
        "A7 handle /x/* { uri path_regexp ^/x/ /; reverse_proxy /v1/* <oq> +observer }": lambda s: s.insert(fb(s) + 1, {"group": "group21",
            "match": [{"path": ["/x/*"]}], "handle": [{"handler": "subroute", "routes": [{"handle": [{"handler": "rewrite", "path_regexp": [
                {"find": "^/x/", "replace": "/"}]}]}, {"match": [{"path": ["/v1/*"]}], "handle": [inject_rp()]}]}]}),
        "A8 handle /x/* { rewrite * /v1{query.p}; reverse_proxy /v1/* <oq> +observer }": lambda s: s.insert(fb(s) + 1, {"group": "group21",
            "match": [{"path": ["/x/*"]}], "handle": [{"handler": "subroute", "routes": [{"group": "group17", "handle": [{"handler": "rewrite",
                "uri": "/v1{http.request.uri.query.p}"}]}, {"match": [{"path": ["/v1/*"]}], "handle": [inject_rp()]}]}]}),
        # wac-099 🔴-2 (review wac-099 C1): intercept's handle_response rewrites and forwards
        "C1 handle /x/* { intercept { handle_response { rewrite * /v1/watcher/dialogs; reverse_proxy <oq> +observer } } respond 404 }":
            lambda s: s.insert(fb(s) + 1, {"group": "group22", "match": [{"path": ["/x/*"]}], "handle": [{"handler": "subroute", "routes": [{"handle": [
                {"handler": "intercept", "handle_response": [{"routes": [{"group": "group17", "handle": [{"handler": "rewrite", "uri": "/v1/watcher/dialogs"}]},
                                                                         {"handle": [inject_rp()]}]}]},
                {"handler": "static_response", "status_code": 404}]}]}]}),
        # isolated: the forwarder inside intercept's handle_response has its own /v1/* matcher, so only the path-change
        # forwarder rule, following intercept, sees it
        "C1b intercept handle_response { rewrite * /v1/watcher/dialogs; reverse_proxy /v1/* <oq> +observer }": lambda s: s.insert(fb(s) + 1, {
            "group": "group22", "match": [{"path": ["/x/*"]}], "handle": [{"handler": "subroute", "routes": [{"handle": [
                {"handler": "intercept", "handle_response": [{"routes": [{"group": "group17", "handle": [{"handler": "rewrite", "uri": "/v1/watcher/dialogs"}]},
                                                                         {"match": [{"path": ["/v1/*"]}], "handle": [inject_rp()]}]}]},
                {"handler": "static_response", "status_code": 404}]}]}]}),
        # isolated: matcher cannot hit, no path change: only _container_check refuses what it cannot follow
        "unknown container on /zzz/* (no path change, matcher misses the prefix)": lambda s: s.insert(fb(s) + 1, {"group": "group21",
            "match": [{"path": ["/zzz/*"]}], "handle": [{"handler": "o0_plugin", "routes": [{"handle": [{"handler": "reverse_proxy",
                "upstreams": [{"dial": "127.0.0.1:8183"}]}]}]}]}),
        "subroute with its own errors routes on /zzz/*": lambda s: s.insert(fb(s) + 1, {"group": "group21", "match": [{"path": ["/zzz/*"]}],
            "handle": [{"handler": "subroute", "routes": [{"handle": [{"handler": "static_response", "status_code": 204}]}],
                        "errors": {"routes": [{"handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": "127.0.0.1:8183"}]}]}]}}]}),
        # review wac-099 E2 / M12: exact-literal path_regexp, CONNECT-only
        "E2 CONNECT ^/v1/watcherx$ + uri strip_suffix x": lambda s: s.insert(0, {"match": [{"method": ["CONNECT"], "path_regexp": {"name": "e2",
            "pattern": "^/v1/watcherx$"}}], "handle": [{"handler": "rewrite", "strip_path_suffix": "x"}]}),
        "reverse_proxy <oq> { rewrite {uri /v1/auth, uri_substring auth -> watcher/dialogs} } on /foo/*": lambda s: s.insert(fb(s) + 1, {
            "group": "group21", "match": [{"path": ["/foo/*"]}], "handle": [{"handler": "reverse_proxy", "rewrite": {"uri": "/v1/auth", "uri_substring": [
                {"find": "auth", "replace": "watcher/dialogs"}]}, "upstreams": [{"dial": "127.0.0.1:8183"}]}]}),
    }
    oq_obs = {"handler": "reverse_proxy", "headers": {"request": {"set": {"Authorization": ["Bearer {env.SYSTEM_OBSERVER_TOKEN}"]}}},
              "upstreams": [{"dial": "127.0.0.1:8183"}]}

    def srv(cfg: dict) -> dict:
        return cfg["apps"]["http"]["servers"]["srv0"]

    def with_errors(cfg: dict, handlers: list, match: list | None = None) -> None:
        inner = {"handle": handlers} if match is None else {"match": match, "handle": handlers}
        srv(cfg)["errors"] = {"routes": [{"match": [{"host": ["jp-bot.balen.wang"]}], "handle": [{"handler": "subroute", "routes": [inner]}],
                                          "terminal": True}]}

    def with_named(cfg: dict, route_fn) -> None:
        srv(cfg)["named_routes"] = {"obs": {"handle": [copy.deepcopy(oq_obs)]}}
        route_fn(_site(cfg))

    # wac-097 (review wac-096 🟡-1): server-level containers - the error chain and named routes reached by invoke
    server_variants = {
        "handle_errors { reverse_proxy <oq> + observer } (review wac-096)": lambda c: with_errors(c, [copy.deepcopy(oq_obs)]),
        "handle_path /x/* { invoke obs }, obs -> <oq> + observer": lambda c: with_named(c, lambda s: s.insert(fb(s) + 1, {"group": "group21",
            "match": [{"path": ["/x/*"]}], "handle": [{"handler": "subroute", "routes": [{"handle": [{"handler": "rewrite", "strip_path_prefix": "/x"}]},
                                                                                         {"handle": [{"handler": "invoke", "name": "obs"}]}]}]})),
        "handle /m/* { invoke obs } after the fallback, obs -> <oq>": lambda c: with_named(c, lambda s: s.insert(fb(s) + 1, {"group": "group21",
            "match": [{"path": ["/m/*"]}], "handle": [{"handler": "invoke", "name": "obs"}]})),
        # isolated: dead behind the fallback, so the emulator (which refuses invoke) never meets it: only the forwarder
        # check's invoke following
        "dead /m/v1/watcher/extra { invoke obs } behind the fallback, obs -> <oq>": lambda c: with_named(c, lambda s: s.insert(fb(s) + 1, {
            "group": "group21", "match": [{"path": ["/m/v1/watcher/extra"]}], "handle": [{"handler": "invoke", "name": "obs"}]})),
        "invoke of an unknown named route on /m/* (uncomparable)": lambda c: _site(c).insert(fb(_site(c)) + 1, {"group": "group21",
            "match": [{"path": ["/m/*"]}], "handle": [{"handler": "invoke", "name": "nope"}]}),
        "handle_errors forwards /api/* to the watcher (basic auth bypass by the error chain)": lambda c: with_errors(c, [{"handler": "reverse_proxy",
            "upstreams": [{"dial": "127.0.0.1:9090"}]}], [{"path": ["/api/*"]}]),
        # review wac-099 E1 (M09): handle_path inside handle_errors
        "E1 handle_errors { handle_path /x/* { reverse_proxy <oq> +observer } respond 404 }": lambda c: with_errors(c, [{"handler": "subroute",
            "routes": [{"match": [{"path": ["/x/*"]}], "handle": [{"handler": "subroute", "routes": [
                {"handle": [{"handler": "rewrite", "strip_path_prefix": "/x"}]}, {"handle": [copy.deepcopy(oq_obs)]}]}]},
                {"handle": [{"handler": "static_response", "status_code": 404}]}]}]),
        # isolated (review M09): the error-chain forwarder has its own /v1/* matcher: only the error chain's path-change rule
        "E1b handle_errors { handle_path /x/* { reverse_proxy /v1/* <oq> +observer } }": lambda c: with_errors(c, [{"handler": "subroute",
            "routes": [{"match": [{"path": ["/x/*"]}], "handle": [{"handler": "subroute", "routes": [
                {"handle": [{"handler": "rewrite", "strip_path_prefix": "/x"}]}, {"match": [{"path": ["/v1/*"]}], "handle": [copy.deepcopy(oq_obs)]}]}]}]}]),
        # review wac-099 M10: a server-level route with two matcher sets, one for another host and one for the site host
        "server route [host other | host site + /x/*] { strip /x; reverse_proxy <oq> }": lambda c: srv(c)["routes"].append({
            "match": [{"host": ["other.example"]}, {"host": ["jp-bot.balen.wang"], "path": ["/x/*"]}], "terminal": True,
            "handle": [{"handler": "subroute", "routes": [{"handle": [{"handler": "rewrite", "strip_path_prefix": "/x"},
                                                                      {"handler": "reverse_proxy", "upstreams": [{"dial": "127.0.0.1:8183"}]}]}]}]}),
        "q2 handle_errors { reverse_proxy <oq> { rewrite ?a=1 } }": lambda c: with_errors(c, [{"handler": "reverse_proxy", "rewrite": {"uri": "?a=1"},
                                                                                          "upstreams": [{"dial": "127.0.0.1:8183"}]}]),
        "q3 handle_errors { reverse_proxy sink { rewrite /ping; handle_response { reverse_proxy <oq> } } }": lambda c: with_errors(c, [{
            "handler": "reverse_proxy", "rewrite": {"uri": "/ping"}, "upstreams": [{"dial": "127.0.0.1:18998"}],
            "handle_response": [{"routes": [{"handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": "127.0.0.1:8183"}]}]}]}]}]),
    }
    caught = 0
    # a handler before the site's subroute inside the host route runs for every request (F-13, enclosing level)
    host_route_extra = copy.deepcopy(good)
    host_route_extra["apps"]["http"]["servers"]["srv0"]["routes"][0]["handle"].insert(0, {"handler": "rewrite", "strip_path_suffix": "/never-there"})
    for form, candidate in (("raw", host_route_extra), ("skeleton", caddy_skeleton(host_route_extra))):
        ok_c, text_c = verdict(candidate)
        assert not ok_c and "container route 0 handler before its subroute" in text_c, ("enclosing-level handler not caught", form)
    checks += 1
    for name, fn in variants.items():
        cfg = mutated(fn)
        for form, candidate in (("raw", cfg), ("skeleton", caddy_skeleton(cfg))):
            ok_variant, text_variant = verdict(candidate)
            if ok_variant:
                print(f"SELFTEST_MISSED {name} ({form})")
                print(text_variant)
                return 1
        caught += 1
    for name, fn in server_variants.items():
        cfg = copy.deepcopy(good)
        fn(cfg)
        for form, candidate in (("raw", cfg), ("skeleton", caddy_skeleton(cfg))):
            ok_variant, text_variant = verdict(candidate)
            if ok_variant:
                print(f"SELFTEST_MISSED {name} ({form})")
                print(text_variant)
                return 1
        caught += 1
    variants_total = len(variants) + len(server_variants)
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
        # wac-097: a case-sensitive regexp that cannot hit, with a path change proven to stay outside (strip_suffix on
        # paths that cannot start with /m/v1/watcher); its strip_prefix twin is now a variant
        "strip_suffix on a case-sensitive regexp that cannot hit": lambda s: s.insert(0, {"match": [{"path_regexp": {"name": "st", "pattern": "^/static/"}}],
            "handle": [{"handler": "rewrite", "strip_path_suffix": "/"}]}),
        # wac-097 (review wac-095 💭-4, p03): a constant rewrite outside the space; the browser sample GET /healthz is
        # emulated through it (/api/status -> the watcher route)
        "p03 rewrite /healthz /api/status (group, before the snippet)": lambda s: s.insert(0, {"group": "group22", "match": [{"path": ["/healthz"]}],
            "handle": [{"handler": "rewrite", "uri": "/api/status"}]}),
        "p05 regexp assets rewritten to a constant": lambda s: s.insert(0, {"group": "group22", "match": [{"path_regexp": {"name": "assets",
            "pattern": "^/static/.*\\.(js|css)$"}}], "handle": [{"handler": "rewrite", "uri": "/static/bundle.js"}]}),
        "p06 uri strip_suffix / on /api/*": lambda s: s.insert(0, {"match": [{"path": ["/api/*"]}], "handle": [{"handler": "rewrite", "strip_path_suffix": "/"}]}),
        "strip_prefix /api on /api/v2/* (cut /v2/* outside)": lambda s: s.insert(0, {"match": [{"path": ["/api/v2/*"]}], "handle": [
            {"handler": "rewrite", "strip_path_prefix": "/api"}]}),
        # (a query-only uri counts as "may hit" since review wac-096 second round 🔴-A: see 1b')
        "method-only rewrite": lambda s: s.insert(0, {"match": [{"path": ["/old/*"]}], "handle": [{"handler": "rewrite", "method": "GET"}]}),
        # wac-099: a matcher after the change that proves the forwarded path outside both spaces; an intercept that only answers
        "handle_path /x/* { reverse_proxy /reports/* <oq> }": lambda s: s.insert(fb(s) + 1, {"group": "group21", "match": [{"path": ["/x/*"]}],
            "handle": [{"handler": "subroute", "routes": [{"handle": [{"handler": "rewrite", "strip_path_prefix": "/x"}]},
                {"match": [{"path": ["/reports/*"]}], "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": "127.0.0.1:8183"}]}]}]}]}),
        # a route that stops (it proxies elsewhere) hands no changed path to later routes of another group
        "stopping handle_path /x/* to another service, then a route-group /v1/* forwarder to <oq>": lambda s: (
            s.insert(fb(s) + 1, {"group": "group99", "match": [{"path": ["/x/*"]}], "handle": [{"handler": "subroute", "routes": [{"handle": [
                {"handler": "rewrite", "strip_path_prefix": "/x"}, {"handler": "reverse_proxy", "upstreams": [{"dial": "127.0.0.1:7000"}]}]}]}]}),
            s.insert(fb(s) + 2, {"match": [{"path": ["/v1/*"]}], "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": "127.0.0.1:8183"}]}]})),
        "intercept /x/* { handle_response { respond 204 } } before the snippet": lambda s: s.insert(0, {"match": [{"path": ["/x/*"]}],
            "handle": [{"handler": "intercept", "handle_response": [{"routes": [{"handle": [{"handler": "static_response", "status_code": 204}]}]}]}]}),
        # wac-097 (review wac-096 🔴-1): forward_auth's own rewrite to a constant outside both spaces, on a matcher that cannot hit
        "forward_auth /bar/* <oq> { uri /v1/auth } before the snippet": lambda s: s.insert(0, {"match": [{"path": ["/bar/*"]}], "handle": [
            {"handler": "reverse_proxy", "rewrite": {"method": "GET", "uri": "/v1/auth"}, "upstreams": [{"dial": "127.0.0.1:8183"}],
             "handle_response": [{"match": {"status_code": [2]}, "routes": [{"handle": [{"handler": "vars"}]}]}]}]}),
        # (handle_path /reports/daily/* would strip all of /reports/daily - real adapt - and fail: see caddy_real_test)
        "handle /reports/daily/* { uri strip_prefix /reports } to operator-query (cut /daily/* outside)": lambda s: s.insert(fb(s) + 1, {"group": "group21",
            "match": [{"path": ["/reports/daily/*"]}], "handle": [{"handler": "subroute", "routes": [{"handle": [
                {"handler": "rewrite", "strip_path_prefix": "/reports"}, {"handler": "reverse_proxy", "upstreams": [{"dial": "127.0.0.1:8183"}]}]}]}]}),
        "not-matcher with encode only": lambda s: s.insert(0, {"match": [{"not": [{"path": ["/x"]}]}], "handle": [{"handler": "encode", "encodings": {"zstd": {}}}]}),
        "other host before the site": lambda s: None,
        "glob rewrite that cannot hit (/static/?ld)": lambda s: s.insert(0, {"match": [{"path": ["/static/?ld"]}], "handle": [
            {"handler": "rewrite", "uri": "/static/old"}]}),
        "'?' in a one-star prefix pattern is literal in Caddy": lambda s: s.insert(0, {"match": [{"path": ["/m/v1/w?tcher*"]}], "handle": [
            {"handler": "rewrite", "uri": "/index.html"}]}),
        "mobile handle spelled localhost:8183": lambda s: s[-4]["handle"][0]["routes"][0]["handle"][1]["upstreams"][0].update({"dial": "localhost:8183"}),
        # wac-094: a suffix glob hits the prefix space, but a response-only header on it is whitelisted (the review's
        # "@static path *.js *.css" expectation); a regexp whose literal prefix rules the space out passes step 1
        "static suffix globs with a response header only": lambda s: s.insert(0, {"match": [{"path": ["*.js", "*.css"]}], "handle": [
            {"handler": "headers", "response": {"set": {"Cache-Control": ["max-age=3600"]}}}]}),
        "rewrite on an anchored regexp with an alternation inside a group": lambda s: s.insert(0, {"match": [{"path_regexp": {"name": "a",
            "pattern": "^/api/(v1|v2)/old$"}}], "handle": [{"handler": "rewrite", "uri": "/api/new"}]}),
        "redir on /m/v1/watcherx* (a fast prefix outside the space)": lambda s: s.insert(0, {"match": [{"path": ["/m/v1/watcherx*"]}], "handle": [
            {"handler": "static_response", "status_code": 308}]}),
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
    # wac-097: server-level containers that must stay quiet: an error page, a named route to another service
    for name, fn in {
            "handle_errors { respond 502 }": lambda c: with_errors(c, [{"handler": "static_response", "status_code": 502}]),
            "named route to another service invoked on /reports/*": lambda c: (srv(c).update({"named_routes": {"rep": {"handle": [
                {"handler": "reverse_proxy", "upstreams": [{"dial": "127.0.0.1:7000"}]}]}}}), _site(c).insert(fb(_site(c)) + 1, {"group": "group21",
                "match": [{"path": ["/reports/*"]}], "handle": [{"handler": "invoke", "name": "rep"}]})),
    }.items():
        cfg = copy.deepcopy(good)
        fn(cfg)
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
        # isolated (G37): CONNECT-only, emulation unaffected; only the prospective shadow check sees it
        "prospective shadow: CONNECT rewrite on /m/v1/watcher/*": lambda s: s.insert(0, {"match": [{"method": ["CONNECT"], "path": ["/m/v1/watcher/*"]}],
            "handle": [{"handler": "rewrite", "strip_path_suffix": "/never-there"}]}),
        # wac-097: the same path-change rules before stage C
        "prospective shadow: CONNECT strip_prefix /x on /x/*": lambda s: s.insert(0, {"match": [{"method": ["CONNECT"], "path": ["/x/*"]}],
            "handle": [{"handler": "rewrite", "strip_path_prefix": "/x"}]}),
        "handle_path /x/* to operator-query before stage C": lambda s: s.insert(1, {"group": "group21", "match": [{"path": ["/x/*"]}],
            "handle": [{"handler": "subroute", "routes": [{"handle": [{"handler": "rewrite", "strip_path_prefix": "/x"},
                                                                     {"handler": "reverse_proxy", "upstreams": [{"dial": "127.0.0.1:8183"}]}]}]}]}),
    }.items():
        bad = copy.deepcopy(base_cfg)
        fn(_site(bad))
        assert not verdict(bad, before_deploy=True)[0], f"before-deploy must flag: {name}"
        checks += 1
    # wac-097 (review wac-095 🟡-2): the probe's rewrite entries - outside the space, and the g15..g18 ones present
    entry_cfg = mutated(lambda s: [s.insert(0, {"match": [{"path": ["/x/*"]}], "handle": [{"handler": "rewrite", "strip_path_prefix": "/x"}]}),
                                   s.insert(0, {"match": [{"path": ["/m/m/*"]}], "handle": [{"handler": "rewrite", "strip_path_prefix": "/m/v1"}]}),
                                   s.insert(0, {"match": [{"path": ["/s/*"]}], "handle": [{"handler": "rewrite", "uri_substring": [
                                       {"find": "/s/", "replace": "/m/v1/watcher/"}]}]}),
                                   s.insert(0, {"group": "g", "match": [{"path": ["/static/*"]}], "handle": [{"handler": "rewrite", "uri": "/m/v1/watcher/status"}]}),
                                   s.insert(0, {"match": [{"path": ["/m/v1/watcherx*"]}], "handle": [{"handler": "rewrite", "strip_path_suffix": "x"}]})])
    entries = rewrite_entry_targets(entry_cfg, "443")
    assert {"/x/m/v1/watcher/status", "/x/m/v1/watcher/trading/accounts", "/s/status", "/s/trading/accounts", "/static/x", "/m/v1/watcherx",
            "/watcher/m/v1/watcher/status", "/m/m/v1/watcher/status", "/m/v1/%2e%2e/m/v1/watcher/status"} <= set(entries), entries
    assert not any(_in_any_space(t) for t in entries) and all(_ENTRY_TARGET.fullmatch(t) for t in entries), entries
    good_entries = rewrite_entry_targets(good, "443")
    all_tails = REWRITE_ENTRY_TAILS + OQ_ENTRY_TAILS
    assert good_entries and set(good_entries) <= {"/m" + t for t in all_tails} | {"/watcher" + t for t in all_tails}, good_entries
    # review wac-096 🔴-1 / 🟡-1: a reverse_proxy's own rewrite into /v1/watcher, invoke and the error chain are followed
    rp_cfg = mutated(lambda s: s.insert(fb(s) + 1, {"group": "group21", "match": [{"path": ["/foo/*"]}], "handle": [{"handler": "subroute", "routes": [
        {"handle": [{"handler": "reverse_proxy", "rewrite": {"uri": "/v1/watcher/dialogs"}, "upstreams": [{"dial": "127.0.0.1:8183"}]}]}]}]}))
    rp_cfg["apps"]["http"]["servers"]["srv0"]["named_routes"] = {"obs": {"handle": [{"handler": "rewrite", "strip_path_prefix": "/n"}]}}
    _site(rp_cfg).insert(0, {"match": [{"path": ["/q/*"]}], "handle": [{"handler": "invoke", "name": "obs"}]})
    rp_cfg["apps"]["http"]["servers"]["srv0"]["errors"] = {"routes": [{"handle": [{"handler": "rewrite", "strip_path_prefix": "/e"}]}]}
    rp_entries = rewrite_entry_targets(rp_cfg, "443")
    assert {"/foo/x", "/n/m/v1/watcher/status", "/n/v1/watcher/dialogs", "/e/v1/watcher/status"} <= set(rp_entries), rp_entries
    checks += 1
    # 5. probe pinning (review wac-088 🟡-3): the config the probe RUNS listens on loopback only and dials only the stubs
    stubs = {"oq": "127.0.0.1:1", "watcher": "127.0.0.1:2", "sink": "127.0.0.1:3"}
    raw = copy.deepcopy(good)
    raw["admin"] = {"disabled": True}
    raw["logging"] = {"logs": {"default": {"writer": {"output": "file", "filename": "/var/log/caddy/x.log"}}}}
    raw["apps"]["tls"] = {"automation": {"policies": [{"subjects": ["jp-bot.balen.wang"]}]}}
    raw["apps"]["http"]["servers"]["srv2"] = {"listen": ["0.0.0.0:8080"], "routes": [{"handle": [
        {"handler": "reverse_proxy", "upstreams": [{"dial": "100.89.58.40:8183"}]}]}]}
    site_r = _site(raw)
    site_r.insert(fb(site_r) + 1, {"group": "group21", "match": [{"path": ["/o0/*"]}], "handle": [{
        "handler": "reverse_proxy", "upstreams": [{"dial": "[::1]:8183"}, {"dial": "unix//run/x.sock"}, {"dial": "localhost:9090", "max_requests": 1}],
        "health_checks": {"active": {"uri": "/h", "upstream": "10.0.0.1:1"}},
        "transport": {"protocol": "http", "network_proxy": {"from": "url", "url": "http://10.0.0.2:3128"}},
        "handle_response": [{"routes": [{"handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": "10.0.0.3:80"}]}]}]}]}]})
    raw_problems = assert_probe_pinned(raw, set(stubs.values()), 443)
    assert any("not a local stub" in x for x in raw_problems) and any("apps must be" in x for x in raw_problems), raw_problems
    pinned, _notes = pin_probe_config(raw, 443, DEFAULT_GATEWAY_UPSTREAM, DEFAULT_WATCHER_UPSTREAM, stubs)
    assert assert_probe_pinned(pinned, set(stubs.values()), 443) == [], assert_probe_pinned(pinned, set(stubs.values()), 443)
    srv = pinned["apps"]["http"]["servers"]
    assert list(srv) == ["srv0"] and srv["srv0"]["listen"] == ["127.0.0.1:443"] and set(pinned) == {"admin", "apps"}, "one loopback server only"
    all_dials: list[str] = []
    _walk_json(pinned, lambda node: all_dials.append(node["dial"]) if "dial" in node else None)
    o0_route = next(r for r in _site(pinned) if (r.get("match") or [{}])[0].get("path") == ["/o0/*"])["handle"][0]
    assert [u["dial"] for u in o0_route["upstreams"]] == [stubs["oq"], stubs["sink"], stubs["watcher"]], o0_route["upstreams"]
    assert o0_route["handle_response"][0]["routes"][0]["handle"][0]["upstreams"] == [{"dial": stubs["sink"]}], "nested reverse_proxy pinned"
    assert "health_checks" not in o0_route and "network_proxy" not in o0_route["transport"], "no own dialing left"
    assert set(all_dials) <= set(stubs.values()) and all_dials.count(stubs["oq"]) == len(lines) + 3, all_dials  # lines, mobile, /v1/*, [::1]:8183
    for name, cfg in (("admin not off", {**copy.deepcopy(raw), "admin": {}}),
                      ("dynamic_upstreams", mutated(lambda s: s.insert(0, {"handle": [{"handler": "reverse_proxy", "dynamic_upstreams": {"source": "srv"}}]})))):
        if name == "dynamic_upstreams":
            cfg["admin"] = {"disabled": True}
        try:
            pin_probe_config(cfg, 443, DEFAULT_GATEWAY_UPSTREAM, DEFAULT_WATCHER_UPSTREAM, stubs)
            raise AssertionError(("probe pinning accepted", name))
        except ArtifactError:
            pass
    checks += 3
    text_probe = probe_caddyfile("{\n\tadmin localhost:2019\n\tdefault_bind 0.0.0.0\n}\njp-bot.balen.wang {\n\trespond 204\n}\n", "jp-bot.balen.wang", 1, 2, 3)
    assert all(f"\t{o}\n" in text_probe for o in ("admin off", "persist_config off", "auto_https off", "default_bind 127.0.0.1")) \
        and "0.0.0.0" not in text_probe and "localhost:2019" not in text_probe, text_probe
    # wac-092 (🟡-3): the probe site keeps the production name (Host-dependent routes behave as in production)
    assert "\nhttp://jp-bot.balen.wang:1 {\n" in text_probe, text_probe
    assert "\nhttp://alias.balen.wang:1 {\n" in probe_caddyfile("jp-bot.balen.wang {\n\trespond 204\n}\n", "jp-bot.balen.wang", 1, 2, 3, "alias.balen.wang")
    # wac-094 (PC-3): site headers spelled with an adapt-time placeholder, a scheme, a port or several addresses
    for header, token in (("{$CADDY_DOMAIN} {", "{$CADDY_DOMAIN}"), ("https://jp-bot.balen.wang {", "https://jp-bot.balen.wang"),
                          ("jp-bot.balen.wang:443 {", "jp-bot.balen.wang:443"), ("jp-bot.balen.wang, alias.balen.wang {", "jp-bot.balen.wang")):
        got = probe_caddyfile("{\n\temail x@example.invalid\n}\n" + header + "\n\trespond 204\n}\n", token, 1, 2, 3, "jp-bot.balen.wang")
        assert "\nhttp://jp-bot.balen.wang:1 {\n" in got and header not in got and "\temail x@example.invalid\n" in got, (header, got)
    try:
        probe_caddyfile("{$CADDY_DOMAIN} {\n\trespond 204\n}\n", "jp-bot.balen.wang", 1, 2, 3)
        raise AssertionError("a site address that is not on any header line must fail, not be guessed")
    except ArtifactError:
        pass
    for bad_host in ("jp-bot.balen.wang/x", "{http.request.host}", "a b", "-x.example"):
        try:
            probe_caddyfile("jp-bot.balen.wang {\n\trespond 204\n}\n", "jp-bot.balen.wang", 1, 2, 3, bad_host)
            raise AssertionError(("probe host accepted", bad_host))
        except ArtifactError:
            pass
    checks += 1
    # R05 (review wac-090 🟡-5): assert_probe_pinned refuses any listener that is not exactly 127.0.0.1:<port>
    for listen in (["0.0.0.0:443"], [":443"], ["127.0.0.1:443", "0.0.0.0:443"], ["[::]:443"]):
        bad_listen = copy.deepcopy(pinned)
        bad_listen["apps"]["http"]["servers"]["srv0"]["listen"] = listen
        assert any("listen" in p for p in assert_probe_pinned(bad_listen, set(stubs.values()), 443)), ("listen accepted", listen)
        checks += 1
    # R10: the probe's Caddy gets no proxy or OTEL_* variable (OTEL_SDK_DISABLED=true is set), whatever the caller exports
    saved_env = dict(os.environ)
    try:
        os.environ.update({"HTTP_PROXY": "http://10.0.0.9:3128", "https_proxy": "http://10.0.0.9:3128", "ALL_PROXY": "socks5://10.0.0.9:1080",
                           "no_proxy": "x", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://10.0.0.9:4317", "OTEL_SDK_DISABLED": "false"})
        penv = _probe_env(Path("/nonexistent-o0-xdg"), ["O0_FAKE=1"])
        leaked = [k for k in penv if k.upper() in PROBE_ENV_DROP or (k.upper().startswith("OTEL_") and k != "OTEL_SDK_DISABLED")]
        assert not leaked, ("proxy/OTEL variable names reach Caddy", leaked)   # names only, never values
        assert penv["OTEL_SDK_DISABLED"] == "true" and penv["HOME"] == "/nonexistent-o0-xdg" and penv["O0_FAKE"] == "1"
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
    checks += 1
    checks += _selftest_probe_process(args, snippet_path)
    print(f"SELFTEST_OK good_passes={passes} variants_caught={caught}/{variants_total} (raw and skeleton) benign_two_step={len(benign)} "
          f"checks={checks} lines={len(lines)} list_format=v2 snippet_verbatim=ok re2_pins=ok caddyfile_text=ok skeleton_verify=ok before_deploy_mode=ok "
          f"probe_pinning=ok matchpath_pins=ok non_ascii=uncomparable double_slash=uncomparable host_matchpins=ok probe_cleanup_signals=ok "
          f"prefix_space=ok probe_residue=ok path_changes=ok re2_classes=ok rewrite_entries=ok")
    return 0


FAKE_CADDY = r'''#!@PYTHON@
# wac-092 selftest stand-in for `caddy` (NOT Caddy): version / adapt / run, with slow modes for the signal tests
import http.server, json, os, re, signal, sys, time
mode, mark = os.environ.get("O0_FAKE_MODE", ""), os.environ["O0_FAKE_MARK"]
def note(s):
    with open(mark, "a") as f:
        f.write(s + "\n")
cmd = sys.argv[1]
if cmd == "version":
    print("v2.10.2 o0-fake"); sys.exit(0)
if cmd == "adapt":
    note(f"adapt {os.getpid()}")
    if "adapt-hang" in mode:
        time.sleep(60); sys.exit(1)
    text = open(sys.argv[sys.argv.index("--config") + 1]).read()
    port = re.search(r"^http://\S+:([0-9]+) \{$", text, re.M).group(1)
    print(json.dumps({"admin": {"disabled": True}, "apps": {"http": {"servers": {"s": {"listen": [":" + port], "routes": []}}}}}))
    sys.exit(0)
if cmd == "run":
    cfg = json.load(open(sys.argv[sys.argv.index("--config") + 1]))
    host, port = cfg["apps"]["http"]["servers"]["s"]["listen"][0].rsplit(":", 1)
    if "slow-stop" in mode:
        def on_term(*_a):
            note("stopping"); time.sleep(60); os._exit(0)
        signal.signal(signal.SIGTERM, on_term)
    class H(http.server.BaseHTTPRequestHandler):
        def _r(self):
            if "slow-requests" in mode:
                note("request"); time.sleep(0.3)
            self.send_response(200); self.send_header("Content-Length", "1"); self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(b"x")
        do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = do_OPTIONS = _r
        def log_message(self, *_a):
            pass
    srv = http.server.ThreadingHTTPServer((host, int(port)), H)
    note(f"run {os.getpid()}")
    srv.serve_forever()
sys.exit(2)
'''


def _selftest_probe_process(args: argparse.Namespace, snippet_path: Path) -> int:
    """wac-092 (review wac-090 🟡-4, 🟡-5 R06/R12): the probe as a separate process against a fake `caddy`
    (so this runs without O0_CADDY_BIN; tests/caddy_real_test.sh repeats the signal cases with the real Caddy).
    Every scenario must leave no .o0probe, an empty TMPDIR (the probe's temporary state dir is gone) and no
    live child process: R06 a probe whose checks FAIL; a SIGTERM while `caddy adapt` runs; a SIGINT during the
    live checks; a SIGTERM that lands while the cleanup waits for Caddy to stop; a SIGHUP during the live checks
    followed by a SIGINT during that cleanup. R12: the jp-24 refusal (Path.exists patched)."""
    import contextlib
    import io
    base = Path(tempfile.mkdtemp(prefix="o0-probe-selftest-"))
    checks = 0
    helpers: list[subprocess.Popen] = []
    try:
        work = base / "copy"
        work.mkdir()
        (work / "Caddyfile").write_text("jp-bot.balen.wang {\n\timport watcher_gateway_routes\n\trespond 204\n}\n", encoding="utf-8")
        shutil.copyfile(snippet_path, work / SNIPPET_FILE)
        fake = base / "caddy"
        fake.write_text(FAKE_CADDY.replace("@PYTHON@", sys.executable), encoding="utf-8")
        fake.chmod(0o700)
        probe_file = work / "Caddyfile.o0probe"

        # R12: refused on a host with /srv/trader-v3, before anything is written
        orig_exists = Path.exists
        Path.exists = lambda self, *a, **k: True if str(self) == "/srv/trader-v3" else orig_exists(self, *a, **k)  # type: ignore[method-assign]
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cmd_probe(build_parser().parse_args(["probe", "--caddy", str(fake), "--caddyfile", str(work / "Caddyfile"),
                                                          "--paths", str(args.paths), "--snippet", str(snippet_path)]))
        finally:
            Path.exists = orig_exists  # type: ignore[method-assign]
        assert rc == 1 and "CADDY_PROBE_REFUSED" in buf.getvalue() and not probe_file.exists(), ("jp-24 refusal", rc, buf.getvalue())
        checks += 1

        def alive(pid: int) -> bool:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
            return True

        def scenario(name: str, mode: str, steps: list[tuple[str, int]], want_rc: int, want_line: str) -> None:
            tmp, mark = base / f"tmp-{name}", base / f"mark-{name}"
            tmp.mkdir()
            mark.write_text("")
            env = {**os.environ, "TMPDIR": str(tmp), "O0_FAKE_MODE": mode, "O0_FAKE_MARK": str(mark)}
            proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "probe", "--caddy", str(fake), "--caddyfile", str(work / "Caddyfile"),
                                     "--paths", str(args.paths), "--snippet", str(snippet_path)],
                                    env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                for wait_for, sig in steps:
                    deadline = time.monotonic() + 30
                    while wait_for not in mark.read_text() and proc.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.02)
                    assert wait_for in mark.read_text(), (name, "never reached", wait_for)
                    proc.send_signal(sig)
                out, _ = proc.communicate(timeout=60)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
            pids = [int(x.split()[1]) for x in mark.read_text().splitlines() if x.split()[0] in ("adapt", "run")]
            deadline = time.monotonic() + 5
            while any(alive(p) for p in pids) and time.monotonic() < deadline:
                time.sleep(0.05)
            problems = []
            if proc.returncode != want_rc or want_line not in out:
                problems.append(f"rc={proc.returncode} (want {want_rc}), line {want_line!r} missing")
            if probe_file.exists():
                problems.append(".o0probe left behind")
            if any(tmp.iterdir()):
                problems.append(f"temporary dir left: {[p.name for p in tmp.iterdir()]}")
            if any(alive(p) for p in pids):
                problems.append(f"child still alive: {pids}")
            if "$2a$" in out:
                problems.append("bcrypt-like text in the output")
            assert not problems, (name, problems, out[-1500:])

        # emergency() on its own (the path a second signal takes): removes .o0probe and the temp dir, exits 128+n
        probe_file.write_text("x")
        em_tmp = base / "em-xdg"
        (em_tmp / "data").mkdir(parents=True)
        (em_tmp / "probe-run.json").write_text("{}")
        em = subprocess.run([sys.executable, "-c", "import pathlib, signal, sys; sys.path.insert(0, sys.argv[1]); import o0_caddy_watcher_routes as r; "
                             "run = r._ProbeRun(pathlib.Path(sys.argv[2]), False); run.xdg = pathlib.Path(sys.argv[3]); run.emergency(signal.SIGTERM)",
                             str(Path(__file__).resolve().parent), str(probe_file), str(em_tmp)], capture_output=True, text=True, timeout=30)
        assert em.returncode == 128 + signal.SIGTERM and "CADDY_PROBE_INTERRUPTED signal=SIGTERM during cleanup" in em.stdout \
            and not probe_file.exists() and not em_tmp.exists(), ("emergency()", em.returncode, em.stdout[-300:], em.stderr[-300:])
        checks += 1
        scenario("R06-checks-fail", "", [], 1, "CADDY_PROBE_FAILED caddy=v2.10.2")
        scenario("SIGTERM-in-adapt", "adapt-hang", [("adapt ", signal.SIGTERM)], 128 + signal.SIGTERM, "CADDY_PROBE_INTERRUPTED signal=SIGTERM:")
        scenario("SIGINT-in-live-checks", "slow-requests", [("request", signal.SIGINT)], 128 + signal.SIGINT, "CADDY_PROBE_INTERRUPTED signal=SIGINT:")
        scenario("SIGTERM-in-cleanup", "slow-stop", [("stopping", signal.SIGTERM)], 128 + signal.SIGTERM,
                 "CADDY_PROBE_INTERRUPTED signal=SIGTERM during cleanup")
        scenario("SIGHUP-then-SIGINT-in-cleanup", "slow-requests slow-stop", [("request", signal.SIGHUP), ("stopping", signal.SIGINT)],
                 128 + signal.SIGINT, "CADDY_PROBE_INTERRUPTED signal=SIGINT during cleanup")
        checks += 5
        checks += _selftest_probe_residue(args, snippet_path, base, work, fake, probe_file, alive, helpers)
    finally:
        for proc in helpers:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        shutil.rmtree(base, ignore_errors=True)
    return checks


def _selftest_probe_residue(args: argparse.Namespace, snippet_path: Path, base: Path, work: Path, fake: Path, probe_file: Path,
                            alive, helpers: list) -> int:
    """wac-094 (review wac-092 🟡-2): SIGKILL of the probe leaves .o0probe, the temporary dir and a running
    (fake) Caddy; the next probe run reports and cleans exactly those (owner record), and leaves alone a
    directory without an owner record, a directory whose probe is still running, and a process it cannot
    prove to be its own. A leftover .o0probe without a record makes the probe refuse to run."""
    checks = 0
    tmp, mark = base / "tmp-sigkill", base / "mark-sigkill"
    tmp.mkdir()
    mark.write_text("")
    cmd = [sys.executable, str(Path(__file__).resolve()), "probe", "--caddy", str(fake), "--caddyfile", str(work / "Caddyfile"),
           "--paths", str(args.paths), "--snippet", str(snippet_path)]
    env = {**os.environ, "TMPDIR": str(tmp), "O0_FAKE_MODE": "slow-requests", "O0_FAKE_MARK": str(mark)}
    proc = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    helpers.append(proc)
    deadline = time.monotonic() + 30
    while "request" not in mark.read_text() and proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "request" in mark.read_text(), "SIGKILL scenario: the live checks never started"
    proc.kill()
    proc.wait()
    run_pid = next(int(x.split()[1]) for x in mark.read_text().splitlines() if x.startswith("run "))
    left = sorted(tmp.glob(PROBE_TMP_PREFIX + "*"))
    assert probe_file.exists() and len(left) == 1 and (left[0] / OWNER_FILE).is_file() and alive(run_pid), \
        ("SIGKILL must leave the residue this test then expects to be cleaned", probe_file.exists(), left, alive(run_pid))
    killed_dir = left[0]
    checks += 1
    # not this tool's (no owner record) and a process that only names the prefix: reported, never touched
    foreign = tmp / (PROBE_TMP_PREFIX + "foreign1")
    foreign.mkdir()
    (foreign / "probe-run.json").write_text("{}")
    ghost = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", str(tmp / (PROBE_TMP_PREFIX + "ghost1") / "probe-run.json")],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    helpers.append(ghost)
    # a directory whose probe is still running (a process whose command line carries the script name)
    busy_owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", PROBE_SCRIPT_MARK + "-busy"],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    helpers.append(busy_owner)
    busy = tmp / (PROBE_TMP_PREFIX + "busy1")
    busy.mkdir()
    (busy / OWNER_FILE).write_text(json.dumps({"format": OWNER_FORMAT, "probe_pid": busy_owner.pid, "probe_file": str(base / "nope.o0probe"),
                                               "probe_file_sha256": None, "pids": []}))
    time.sleep(0.2)
    mark.write_text("")
    env2 = {**env, "O0_FAKE_MODE": ""}
    out = subprocess.run(cmd, env=env2, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120).stdout
    deadline = time.monotonic() + 5
    while alive(run_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    problems = []
    if not re.search(rf"^PROBE_RESIDUE_CLEANED dir={re.escape(killed_dir.name)} processes_stopped=1 o0probe=removed dir_removed=True$", out, re.M):
        problems.append("no CLEANED line for the killed probe's dir")
    if alive(run_pid):
        problems.append("the killed probe's Caddy is still running")
    if killed_dir.exists():
        problems.append("the killed probe's dir is still there")
    if not re.search(rf"^PROBE_RESIDUE_FOREIGN dir={PROBE_TMP_PREFIX}foreign1: ", out, re.M) or not (foreign / "probe-run.json").exists():
        problems.append("foreign dir not reported or touched")
    if not re.search(rf"^PROBE_RESIDUE_BUSY dir={PROBE_TMP_PREFIX}busy1: its probe \(pid {busy_owner.pid}\)", out, re.M) or not busy.exists():
        problems.append("busy dir not reported or touched")
    if not re.search(rf"^PROBE_RESIDUE_ORPHAN pid={ghost.pid}: ", out, re.M) or ghost.poll() is not None:
        problems.append("orphan not reported or touched")
    if not re.search(r"^PROBE_RESIDUE_SCAN cleaned=1 busy=1 foreign=1 orphans=[1-9][0-9]* unproven=0$", out, re.M):
        problems.append("scan summary")
    if "CADDY_PROBE_FAILED caddy=v2.10.2" not in out:     # the fake Caddy answers 200 everywhere: checks fail, as in R06
        problems.append("the second run did not run to the end")
    if probe_file.exists() or sorted(p.name for p in tmp.iterdir()) != sorted([foreign.name, busy.name]):
        problems.append(f"after the second run: o0probe={probe_file.exists()} tmp={sorted(p.name for p in tmp.iterdir())}")
    assert not problems, ("residue scan", problems, out[-2500:])
    checks += 1
    # a .o0probe without a record: refuse (it may be a --keep copy the user still reads), leave it alone
    shutil.rmtree(foreign)
    shutil.rmtree(busy)
    probe_file.write_text("left by --keep")
    r = subprocess.run(cmd, env=env2, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "CADDY_PROBE_FAILED Caddyfile.o0probe already exists" in r.stdout and probe_file.read_text() == "left by --keep" \
        and not any(tmp.iterdir()), ("leftover .o0probe must stop the probe", r.returncode, r.stdout[-800:])
    probe_file.unlink()
    checks += 1
    # a recorded .o0probe whose content changed since (not provably ours any more): the dir goes, the file stays
    d = tmp / (PROBE_TMP_PREFIX + "stale1")
    d.mkdir()
    probe_file.write_text("edited")
    (d / OWNER_FILE).write_text(json.dumps({"format": OWNER_FORMAT, "probe_pid": 2 ** 22 + 7, "probe_file": str(probe_file),
                                            "probe_file_sha256": hashlib.sha256(b"original").hexdigest(), "pids": []}))
    lines: list[str] = []
    scan_probe_residue(tmp, out=lines.append)
    assert any(l.startswith(f"PROBE_RESIDUE_CLEANED dir={d.name} processes_stopped=0 o0probe=kept") for l in lines) and probe_file.exists() \
        and not d.exists(), ("changed .o0probe must be kept", lines)
    probe_file.unlink()
    checks += 1
    for h in (ghost, busy_owner):
        h.kill()
        h.wait()
    # the owner record exists as soon as the temporary dir does (a SIGKILL before the first child still leaves a
    # directory the next scan can prove and remove)
    saved_tempdir = tempfile.tempdir
    tempfile.tempdir = str(tmp)
    try:
        r0 = _ProbeRun(base / "never.o0probe", False)
        x0 = r0.mkdtemp()
        rec = json.loads((x0 / OWNER_FILE).read_text())
        assert rec["format"] == OWNER_FORMAT and rec["probe_pid"] == os.getpid() and rec["pids"] == [] and rec["probe_file_sha256"] is None, rec
        shutil.rmtree(x0)
    finally:
        tempfile.tempdir = saved_tempdir
    checks += 1
    # a process that names the dir but is NOT a recorded group leader and ignores SIGTERM: SIGTERM first, SIGKILL after 5 s
    d = tmp / (PROBE_TMP_PREFIX + "stubborn1")
    d.mkdir()
    (d / OWNER_FILE).write_text(json.dumps({"format": OWNER_FORMAT, "probe_pid": 2 ** 22 + 9, "probe_file": str(base / "none.o0probe"),
                                            "probe_file_sha256": None, "pids": []}))
    note = base / "stubborn-note"
    stubborn = subprocess.Popen([sys.executable, "-c", "import signal, sys, time\n"
                                 "signal.signal(signal.SIGTERM, lambda *a: open(sys.argv[1], 'a').write('term\\n'))\n"
                                 "open(sys.argv[1], 'a').write('ready\\n')\ntime.sleep(120)", str(note), str(d / "probe-run.json")],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    helpers.append(stubborn)
    deadline = time.monotonic() + 10
    while not (note.exists() and "ready" in note.read_text()) and time.monotonic() < deadline:
        time.sleep(0.02)
    lines = []
    scan_probe_residue(tmp, out=lines.append)
    stubborn.wait(timeout=10)
    assert any(l.startswith(f"PROBE_RESIDUE_CLEANED dir={d.name} processes_stopped=1 ") for l in lines) and "term" in note.read_text() \
        and stubborn.returncode == -signal.SIGKILL and not d.exists(), ("SIGTERM then SIGKILL", lines, note.read_text(), stubborn.returncode)
    checks += 1
    # SIGKILL of the probe while `caddy adapt` hangs: the orphan adapt names only the .o0probe; the next scan stops it
    # because its pid is RECORDED, and removes the .o0probe (recorded sha) and the dir
    mark.write_text("")
    proc = subprocess.Popen(cmd, env={**env, "O0_FAKE_MODE": "adapt-hang"}, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    helpers.append(proc)
    deadline = time.monotonic() + 30
    while "adapt " not in mark.read_text() and proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    proc.kill()
    proc.wait()
    adapt_pid = next(int(x.split()[1]) for x in mark.read_text().splitlines() if x.startswith("adapt "))
    left = sorted(tmp.glob(PROBE_TMP_PREFIX + "*"))
    assert alive(adapt_pid) and probe_file.exists() and len(left) == 1, ("adapt orphan setup", alive(adapt_pid), probe_file.exists(), left)
    lines = []
    scan_probe_residue(tmp, out=lines.append)
    deadline = time.monotonic() + 5
    while alive(adapt_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert any(l == f"PROBE_RESIDUE_CLEANED dir={left[0].name} processes_stopped=1 o0probe=removed dir_removed=True" for l in lines) \
        and not alive(adapt_pid) and not probe_file.exists() and not any(tmp.iterdir()), ("adapt orphan", lines)
    checks += 1
    return checks


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
    p.add_argument("--site-address", default="jp-bot.balen.wang", help="the site address token on the site header line of the copy")
    p.add_argument("--host", default="", help="production host name the probe site keeps and every probe request sends as Host (default: --site-address)")
    p.add_argument("--upstream", default=DEFAULT_GATEWAY_UPSTREAM, help="operator-query as the copy writes it (any loopback spelling maps to the stub)")
    p.add_argument("--watcher-upstream", default=DEFAULT_WATCHER_UPSTREAM)
    p.add_argument("--adapt-env", action="append", help="NAME=VALUE for {$NAME} adapt-time placeholders (fake values only)")
    p.add_argument("--keep", action="store_true", help="keep <copy>.o0probe (it contains the bcrypt hash: delete it yourself)")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("selftest")
    p.add_argument("--paths", type=Path, default=default_paths)
    p.add_argument("--snippet", type=Path, help=snippet_help)
    p.set_defaults(func=cmd_selftest)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except _ProbeInterrupted as exc:     # a signal outside the probe's own try (its finally already cleaned up)
        print(f"CADDY_PROBE_INTERRUPTED signal={signal.Signals(exc.signum).name}")
        return 128 + exc.signum


if __name__ == "__main__":
    raise SystemExit(main())
