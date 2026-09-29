#!/usr/bin/env python3
"""O-0 small helpers shared by the o0_*.sh drafts (no secrets are ever printed).

  env-exec         run a command with KEY=VALUE env files loaded WITHOUT shell
                   expansion (Caddy v3.env holds a bcrypt hash with '$'; sourcing
                   it in a shell corrupts it, see memory jp24 caddy notes)
  redact           stdin -> stdout, masks bearer values, bcrypt hashes, secret-looking
                   KEY=VALUE values and long opaque tokens
  manifest-build   sha256 manifest for a list of relative paths (ABSENT allowed)
  manifest-verify  verify a directory against a manifest; 0 compared = failure
  sqlite-backup    consistent online copy of a live SQLite DB (backup API, source
                   opened read-only), integrity_check on the copy, row counts only
  http-probe       HTTP request that may carry a token read from an env file; prints
                   only status, `code` and selected headers, never the token or body
  closure          watcher require() closure from server.js vs the image whitelist
  sums-compare     compare `sha256sum` output captured on jp-24 with a baseline manifest
  redact-json      stdin JSON -> stdout JSON, two layers: (1) deny-by-default in every
                   secret-bearing context (password, any header set/add/replace/matcher
                   value, static_response body, vars, forward_auth, transport, tls,
                   secret-looking key names) and (2) every remaining string through the
                   bearer / bcrypt / long-opaque line rules; {placeholders} are kept
  caddy-skeleton   stdin Caddy JSON -> ALLOWLISTED structure only (matchers, handler
                   order, rewrite, dials, header NAMES, {placeholder} values); anything
                   else becomes <literal len=N>. This is what leaves jp-24 (site check).
  fleet-compare    fleet samples (docs/agent-operations.md §0 query + /ready): exact node
                   set, heartbeat age < 5 s, status / release / ready unchanged;
                   0 rows or a partial node set = UNCOMPARABLE (exit 2), change = exit 3
  gate-write       machine-readable gate result bound to a bundle (candidate, SHA256SUMS
                   digest, input file digests); written only at the end of a passed gate
  gate-check       apply-side check of a gate file: present, same stage, same candidate
                   and bundle, same input digests, fresh enough; else refuse
  warmup-check     C-0 per-worker warm-up lines (wac-041 `snapshot_warmup`) from a journal
                   excerpt: exactly N distinct pids, all success, revision >= R0
  selftest         offline tests of the helpers above
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import http.client
import json
import os
import re
import shlex
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SECRET_KEY_RE = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|HASH|SESSION|API_KEY|APIKEY|PRIVATE|AUTH_JSON|CREDENTIAL)", re.I)


def parse_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            raise SystemExit(f"{path.name}:{lineno}: not KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not KEY_RE.match(key):
            raise SystemExit(f"{path.name}:{lineno}: invalid key")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key] = value
    return result


def cmd_env_exec(args: argparse.Namespace) -> int:
    if not args.command:
        raise SystemExit("env-exec: missing command after --")
    command = args.command[1:] if args.command[0] == "--" else args.command
    env = {"PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin"), "HOME": os.environ.get("HOME", "/root")} if args.clear_env else dict(os.environ)
    for path in args.env_file or []:
        env.update(parse_env_file(path))
    os.execvpe(command[0], command, env)
    return 127


REDACTIONS = (
    (re.compile(r"\$2[abxy]?\$\d{2}\$[./A-Za-z0-9]{53}"), "<bcrypt-hash>"),
    (re.compile(r"(?i)(bearer\s+)(?!\{)[^\s\"'}]+"), r"\1<redacted>"),
    (re.compile(r"(?i)((?:basic_?auth|basicauth)\s+\S+\s+)(?!\{)\S+"), r"\1<redacted>"),
    # query-string values (?token=..., &key=...): never needed for review
    (re.compile(r"([?&][A-Za-z0-9_.-]+=)(?!\{)[^&\s\"'#]+"), r"\1<redacted>"),
)
# handlers whose every field (except the handler name) is data, never structure
SENSITIVE_HANDLERS = {"vars", "static_response", "forward_auth", "authentication", "map", "templates", "push"}
LONG_OPAQUE = re.compile(r"(?<![A-Za-z0-9_./-])(?=[A-Za-z0-9_+=-]*[0-9])(?=[A-Za-z0-9_+=-]*[A-Za-z])[A-Za-z0-9_+=-]{28,}(?![A-Za-z0-9_./-])")
KV = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*[=:]\s*)(.*)$")
# "Path as a secret" (review wac-032-r2 🔴-1): probe tokens, webhook tokens, bot tokens and
# rewrite targets sit INSIDE paths, where LONG_OPAQUE (it does not look behind '/') cannot
# see them. A token is ">= 12 chars with a letter and a digit" or ">= 16 hex digits".
# Two passes (review wac-032-r3 🟡-1: escapes, '.', '-' and '+' cut one token into short
# pieces that each stayed below the threshold):
# 1. the path is split at '/' into chunks; a chunk is judged twice, with {placeholders} and
#    regex syntax removed, and with separators AND '+'/'=' removed as well ('+' and '=' are
#    base64 characters: the first pass keeps them, the second drops them, review wac-072 🟡-2):
#    the first catches short dotted parts that only reach 12 with their separators, the second
#    catches separated digit/hex tokens ('-', '.', '+', '=' ...); if either is a token,
#    the WHOLE chunk becomes <seg len=N>;
# 2. any other chunk is split at regex metacharacters and each piece is judged as before.
PATH_CHUNK_SPLIT = re.compile(r"(/+)")
PATH_SEG_SPLIT = re.compile(r"([/^$.*+?()\[\]{}|\\]+)")
PATH_WORD = re.compile(r"[^\s\"']*/[^\s\"']*")
CHUNK_PLACEHOLDER = re.compile(r"\{[A-Za-z0-9_.$:-]+\}")
CHUNK_REGEX_SYNTAX = re.compile(r"\[[^\]]*\][*+?]?|\(\?[A-Za-z:]*|[\\^$()|*?{}\[\]]")
CHUNK_SEPARATORS = re.compile(r"[.\-_:~%&,;!@]")


def _token_segment(seg: str) -> bool:
    if len(seg) >= 12 and re.search(r"[A-Za-z]", seg) and re.search(r"[0-9]", seg):
        return True
    return re.fullmatch(r"[0-9A-Fa-f]{16,}", seg) is not None


def _chunk_is_token(chunk: str) -> bool:
    bare = CHUNK_REGEX_SYNTAX.sub("", CHUNK_PLACEHOLDER.sub("", chunk))
    # second pass also drops '+' and '=' (review wac-072 🟡-2: '4829+1057+3829+1045' is a 16-digit
    # token); the first pass keeps them as base64 token characters (N7 is unaffected)
    return _token_segment(bare) or _token_segment(CHUNK_SEPARATORS.sub("", bare).replace("+", "").replace("=", ""))


def _redact_pieces(chunk: str) -> str:
    parts = PATH_SEG_SPLIT.split(chunk)
    return "".join(f"<seg len={len(p)}>" if i % 2 == 0 and p and _token_segment(p) else p for i, p in enumerate(parts))


def redact_path(value: str) -> str:
    out = []
    for i, chunk in enumerate(PATH_CHUNK_SPLIT.split(value)):
        if i % 2 == 1 or not chunk:
            out.append(chunk)
        elif _chunk_is_token(chunk):
            out.append(f"<seg len={len(chunk)}>")
        else:
            out.append(_redact_pieces(chunk))
    return "".join(out)


def redact_line(line: str) -> str:
    if line.lstrip().startswith("Environment="):
        names = [a.split("=", 1)[0].strip('"') for a in line.split("=", 1)[1].split()]
        return "Environment=<names:" + ",".join(names) + ">"
    match = KV.match(line)
    if match and SECRET_KEY_RE.search(match.group(2)) and match.group(4).strip() and not match.group(4).strip().startswith("{"):
        line = f"{match.group(1)}{match.group(2)}{match.group(3)}<redacted len={len(match.group(4).strip())}>"
    for pattern, repl in REDACTIONS:
        line = pattern.sub(repl, line)
    line = LONG_OPAQUE.sub("<redacted-opaque>", line)
    # every word that contains '/' is treated as a path (Caddyfile diff lines, URLs, globs)
    return PATH_WORD.sub(lambda m: redact_path(m.group(0)), line)


def cmd_redact(_args: argparse.Namespace) -> int:
    for line in sys.stdin:
        sys.stdout.write(redact_line(line.rstrip("\n")) + "\n")
    return 0


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cmd_manifest_build(args: argparse.Namespace) -> int:
    paths = [p.strip() for p in args.paths_file.read_text(encoding="utf-8").splitlines() if p.strip() and not p.startswith("#")]
    if not paths:
        raise SystemExit("manifest-build: 0 paths")
    lines = []
    for rel in paths:
        if rel.startswith("/") or ".." in Path(rel).parts:
            raise SystemExit(f"manifest-build: unsafe path {rel}")
        target = args.root / rel
        lines.append(f"{sha256_file(target) if target.is_file() else 'ABSENT'}  {rel}")
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"MANIFEST_BUILT entries={len(lines)} out={args.out}")
    return 0


def cmd_manifest_verify(args: argparse.Namespace) -> int:
    compared = mismatches = 0
    for raw in args.manifest.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        expected, rel = raw.split("  ", 1)
        target = args.root / rel
        actual = sha256_file(target) if target.is_file() else "ABSENT"
        compared += 1
        if actual == expected:
            if args.verbose:
                print(f"MATCH {rel}")
        else:
            mismatches += 1
            print(f"MISMATCH {rel} expected={expected[:12]} actual={actual[:12]}")
    label = args.label or args.manifest.name
    if compared == 0:
        print(f"MANIFEST_UNCOMPARABLE {label} compared=0")
        return 1
    if mismatches:
        print(f"MANIFEST_MISMATCH {label} compared={compared} mismatches={mismatches}")
        return 1
    print(f"MANIFEST_OK {label} compared={compared}")
    return 0


def cmd_sqlite_backup(args: argparse.Namespace) -> int:
    if args.dest.exists():
        raise SystemExit(f"sqlite-backup: destination exists: {args.dest}")
    src = sqlite3.connect("file:" + str(args.source) + "?mode=ro", uri=True)
    fd = os.open(args.dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    dst = sqlite3.connect(str(args.dest))
    try:
        src.backup(dst)
    finally:
        src.close()
    # The backup API reads committed WAL frames of the source. The copy inherits the
    # source's WAL flag; switch it to a rollback journal so every later reader of the
    # copy (read-only, possibly in a read-only directory) sees one self-contained file.
    mode = dst.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
    if str(mode).lower() != "delete":
        raise SystemExit(f"sqlite-backup: could not switch the copy to journal_mode=delete (got {mode})")
    integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
    counts = []
    for table in ("account_configs", "channel_routing", "symbol_risk_configs", "config_revision", "config_audit", "telegram_messages", "price_alerts"):
        if dst.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            counts.append(f"{table}={dst.execute(f'SELECT count(*) FROM {table}').fetchone()[0]}")
        else:
            counts.append(f"{table}=ABSENT")
    dst.close()
    os.chmod(args.dest, 0o600)
    print(f"SQLITE_BACKUP dest={args.dest} integrity={integrity} sha256={sha256_file(args.dest)} {' '.join(counts)}")
    return 0 if integrity == "ok" else 1


def cmd_http_probe(args: argparse.Namespace) -> int:
    parts = urlsplit(args.url)
    if parts.scheme not in ("http", "https"):
        raise SystemExit("http-probe: http(s) only")
    headers = {"Accept": "application/json", "User-Agent": "o0-probe"}
    if args.token_env_file:
        value = parse_env_file(args.token_env_file).get(args.token_var or "")
        if not value:
            print(f"PROBE_FAIL token variable {args.token_var} not configured in {args.token_env_file.name}")
            return 1
        if args.proxy_header:
            headers[args.proxy_header] = value
        else:
            headers["Authorization"] = "Bearer " + value
    elif args.bogus_bearer:
        headers["Authorization"] = "Bearer " + "o0-probe-not-a-token-" + "0" * 24
    for item in args.header or []:
        name, _, val = item.partition(":")
        headers[name.strip()] = val.strip()
    conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(parts.hostname, parts.port, timeout=args.timeout)
    path = parts.path + (("?" + parts.query) if parts.query else "")
    try:
        conn.request(args.method, path or "/", headers=headers)
        resp = conn.getresponse()
        body = resp.read(65536)
    except OSError as exc:
        print(f"PROBE {args.method} {args.url} error={type(exc).__name__}")
        return 1 if args.expect_status else 0
    code = ""
    fields = ""
    parsed_obj = None
    try:
        parsed = json.loads(body.decode("utf-8"))
        parsed_obj = parsed
        if isinstance(parsed, dict):
            code = str(parsed.get("code") or parsed.get("detail") or "")[:60]
            allowed = {"revision", "content_sha256", "schema_version", "generated_at", "snapshot_state", "observed_at", "connection", "listener",
                       "status", "app_role", "database", "gateway"}
            wanted = [f for f in (args.show_json_field or []) if f in allowed]
            fields = " ".join(f"{f}={parsed.get(f)}" for f in wanted)
    except (ValueError, UnicodeDecodeError):
        code = "<non-json>"
    shown = " ".join(f"{h}={resp.getheader(h)}" for h in (args.show_header or []) if resp.getheader(h) is not None)
    print(f"PROBE {args.method} {args.url} status={resp.status} code={code} {shown} {fields}".rstrip())
    ok = True
    if args.expect_status and resp.status not in args.expect_status:
        ok = False
    if args.expect_code and code != args.expect_code:
        ok = False
    if args.expect_no_location and resp.getheader("Location"):
        ok = False
    for kv in args.expect_json or []:
        k, _, v = kv.partition("=")
        if k not in ("status", "app_role", "database", "gateway") or not isinstance(parsed_obj, dict) or str(parsed_obj.get(k)) != v:
            ok = False
            print(f"PROBE_FAIL json {k} != {v}")
    if not ok:
        print(f"PROBE_FAIL expected status={args.expect_status} code={args.expect_code}")
        return 1
    return 0


REQUIRE_RE = re.compile(r"require\(\s*[\"'](\.{1,2}/[^\"']+)[\"']\s*\)")


def cmd_closure(args: argparse.Namespace) -> int:
    root = args.watcher_root
    seen: set[str] = set()
    stack = ["server.js", "price-monitor.js"]
    while stack:
        rel = stack.pop()
        if rel in seen:
            continue
        seen.add(rel)
        text = (root / rel).read_text(encoding="utf-8")
        for spec in REQUIRE_RE.findall(text):
            target = (Path(rel).parent / spec).as_posix()
            target = os.path.normpath(target)
            candidates = [target, target + ".js", target + "/index.js"]
            found = next((c for c in candidates if (root / c).is_file()), None)
            if found is None:
                print(f"CLOSURE_MISSING_FILE {rel} requires {spec}")
                return 1
            stack.append(found)
    tree = ast.parse(args.builder.read_text(encoding="utf-8"))
    whitelist: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "WATCHER_RUNTIME_RELATIVE_PATHS" for t in node.targets):
            whitelist = set(ast.literal_eval(node.value))
    if not whitelist:
        print("CLOSURE_UNCOMPARABLE whitelist not found")
        return 1
    missing = sorted(seen - whitelist)
    for rel in missing:
        print(f"CLOSURE_NOT_WHITELISTED {rel}")
    if missing:
        print(f"CLOSURE_FAILED closure={len(seen)} whitelist={len(whitelist)} missing={len(missing)}")
        return 1
    print(f"CLOSURE_OK closure={len(seen)} whitelist={len(whitelist)}")
    return 0


def cmd_sums_compare(args: argparse.Namespace) -> int:
    live: dict[str, str] = {}
    prefix = args.prefix.rstrip("/") + "/"
    for raw in args.sums.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([0-9a-f]{64})\s+\*?(.+)$", raw.strip())
        if match and match.group(2).startswith(prefix):
            live[match.group(2)[len(prefix):]] = match.group(1)
    compared = mismatches = 0
    expected_paths = set()
    for raw in args.manifest.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        expected, rel = raw.split("  ", 1)
        expected_paths.add(rel)
        actual = live.get(rel, "ABSENT")
        compared += 1
        if actual != expected:
            mismatches += 1
            print(f"DRIFT {rel} baseline={expected[:12]} live={actual[:12]}")
    for rel in sorted(set(live) - expected_paths):
        print(f"INFO live-only {rel} live={live[rel][:12]}")
    label = args.label or args.manifest.name
    if compared == 0 or not live:
        print(f"SUMS_UNCOMPARABLE {label} compared={compared} live_entries={len(live)}")
        return 1
    if mismatches:
        print(f"SUMS_DRIFT {label} compared={compared} drift={mismatches}")
        return 1
    print(f"SUMS_OK {label} compared={compared}")
    return 0


# ---------------------------------------------------------------- JSON redaction
PLACEHOLDER_RE = re.compile(r"^(Bearer |Basic )?\{[A-Za-z0-9_.$:-]+\}$")
HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")
SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_@.:*/+-]{1,64}$")
# Contexts where EVERY string value is secret-bearing unless it is a {placeholder}.
SENSITIVE_KEYS = {
    "password", "headers", "header", "header_regexp", "body", "vars", "vars_regexp",
    "forward_auth", "transport", "tls", "client_authentication", "credentials",
    "hash", "salt", "secret", "api_key", "token", "authorization", "cookie",
    "set", "add", "replace", "query", "expression", "request_body",
}


def _literal(value) -> str:
    return f"<literal len={len(str(value))}>"


def _redact_key(key: str) -> str:
    if SAFE_KEY_RE.match(key) and LONG_OPAQUE.search(key) is None and redact_line(key) == key:
        return key
    return f"<key len={len(key)}>"


def _redact_json(node, sensitive: bool = False, parent: str = ""):
    """Two layers: (1) sensitive context -> <literal len=N> unless placeholder;
    (2) any other string -> redact_line (bearer, bcrypt, long opaque, KEY=VALUE)."""
    if isinstance(node, dict):
        out = {}
        data_handler = node.get("handler") in SENSITIVE_HANDLERS
        for key, value in node.items():
            here_sensitive = (sensitive or key.lower() in SENSITIVE_KEYS or SECRET_KEY_RE.search(key) is not None
                              or (data_handler and key not in ("handler", "status_code", "providers", "http_basic")))
            out[_redact_key(key)] = _redact_json(value, here_sensitive, key.lower())
        return out
    if isinstance(node, list):
        return [_redact_json(v, sensitive, parent) for v in node]
    if isinstance(node, str):
        if PLACEHOLDER_RE.match(node):
            return node
        if sensitive:
            if parent == "delete" and HEADER_NAME_RE.match(node) and redact_line(node) == node:
                return node  # header NAMES to delete are structure, not values
            return _literal(node)
        red = redact_line(node)
        if red == node:
            return node
        if parent in SKELETON_PATH_KEYS and red == redact_path(node):
            return red  # only token-shaped path segments were replaced: keep the route shape
        return _literal(node)
    return node


def cmd_redact_json(_args: argparse.Namespace) -> int:
    json.dump(_redact_json(json.load(sys.stdin)), sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
    return 0


# Allowlist skeleton of a Caddy JSON config: only what the route verifier and the
# O-0 checklist need. The same function is embedded verbatim in o0_site_check.sh
# (O0_SKELETON_PY); site_check_leak_test.sh asserts both give identical output.
SKELETON_STRING_KEYS = {
    "handler", "dial", "listen", "host", "path", "method", "strip_path_prefix",
    "strip_path_suffix", "group", "protocol", "name", "pattern", "@id", "root",
    "flush_interval", "uri",
}
# path-like values: token-shaped segments are replaced by <seg len=N>, the rest is kept
SKELETON_PATH_KEYS = {"path", "pattern", "uri", "root", "strip_path_prefix", "strip_path_suffix"}


def caddy_skeleton(node, ctx: tuple = ()):
    parent = ctx[-1] if ctx else ""
    header_ctx = any(c in ("headers", "request_header", "header_up", "header_down") for c in ctx)
    matcher_ctx = any(c in ("header", "header_regexp", "vars", "vars_regexp", "expression", "query", "remote_ip", "client_ip") for c in ctx)
    secret_ctx = any(c in ("password", "body", "vars", "forward_auth", "transport", "tls", "credentials", "accounts") for c in ctx)
    if isinstance(node, dict):
        out = {}
        kind = node.get("handler")
        extra = ("headers",) if kind == "headers" else (("body",) if kind in ("vars", "map", "templates") else ())
        for key, value in node.items():
            k = key if (SAFE_KEY_RE.match(key) and LONG_OPAQUE.search(key) is None and redact_line(key) == key) else f"<key len={len(key)}>"
            sub = ctx + (extra if key != "handler" else ()) + (key.lower(),)
            out[k] = caddy_skeleton(value, sub)
        return out
    if isinstance(node, list):
        return [caddy_skeleton(v, ctx) for v in node]
    if isinstance(node, bool) or node is None:
        return node
    if isinstance(node, (int, float)):
        return node if not secret_ctx else _literal(node)
    if not isinstance(node, str):
        return _literal(node)
    if PLACEHOLDER_RE.match(node):
        return node
    if secret_ctx or matcher_ctx:
        return _literal(node)
    if header_ctx:
        if parent == "delete" and HEADER_NAME_RE.match(node) and redact_line(node) == node:
            return node
        return _literal(node)
    if parent in SKELETON_PATH_KEYS:
        node = redact_path(node)
    if parent in SKELETON_STRING_KEYS and redact_line(node) == node and len(node) <= 256:
        return node
    return _literal(node)


def cmd_caddy_skeleton(_args: argparse.Namespace) -> int:
    json.dump(caddy_skeleton(json.load(sys.stdin)), sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
    return 0


# ---------------------------------------------------------------- fleet guard
FLEET_NODE_RE = re.compile(r"^(\S+) (\S+) (\S+) hb_age=(-?[0-9]+(?:\.[0-9]+)?)$")
FLEET_READY_RE = re.compile(r"^ready:([0-9]+) ([0-9]{3})$")


class FleetUncomparable(Exception):
    pass


def parse_fleet(path: Path) -> tuple[dict, dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FleetUncomparable(f"{path.name}: unreadable ({type(exc).__name__})") from exc
    nodes: dict[str, tuple[str, str, float]] = {}
    ready: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = FLEET_NODE_RE.match(line)
        r = FLEET_READY_RE.match(line)
        if m:
            if m.group(1) in nodes:
                raise FleetUncomparable(f"{path.name}: duplicate node {m.group(1)}")
            nodes[m.group(1)] = (m.group(2), m.group(3), float(m.group(4)))
        elif r:
            ready[r.group(1)] = r.group(2)
        else:
            raise FleetUncomparable(f"{path.name}: unparsable line {line[:60]!r}")
    if not nodes:
        raise FleetUncomparable(f"{path.name}: 0 node rows")
    return nodes, ready


def fleet_compare(before: Path, after: Path | None, expected_nodes: list[str], ready_ports: list[str],
                  max_age: float, max_jump: float, known_down: list[str]) -> tuple[int, list[str]]:
    lines: list[str] = []
    try:
        b_nodes, b_ready = parse_fleet(before)
        a_nodes, a_ready = parse_fleet(after) if after is not None else (None, None)
    except FleetUncomparable as exc:
        return 2, [f"FLEET_UNCOMPARABLE {exc}"]
    expected = set(expected_nodes)
    if not expected:
        return 2, ["FLEET_UNCOMPARABLE expected node set is empty"]
    for label, nodes, rdy in (("before", b_nodes, b_ready), ("after", a_nodes, a_ready)):
        if nodes is None:
            continue
        if set(nodes) != expected:
            return 2, [f"FLEET_UNCOMPARABLE {label}: node set {sorted(nodes)} != expected {sorted(expected)}"]
        if set(rdy) != set(ready_ports):
            return 2, [f"FLEET_UNCOMPARABLE {label}: ready ports {sorted(rdy)} != expected {sorted(ready_ports)}"]
    down = set(known_down)
    stale = [n for n in sorted(expected - down) if b_nodes[n][2] >= max_age]
    if stale:
        return 2, [f"FLEET_BASELINE_STALE heartbeat age >= {max_age}s before the change: {','.join(stale)} "
                   "(a frozen heartbeat means the node is dead, not unchanged; fix the fleet first)"]
    if a_nodes is None:
        for n in sorted(expected):
            lines.append(f"node {n} status={b_nodes[n][0]} release={b_nodes[n][1]} hb_age={b_nodes[n][2]}")
        lines.append(f"FLEET_BASELINE_OK nodes={len(expected)} ready_ports={len(ready_ports)} max_hb_age={max_age}")
        return 0, lines
    changed = []
    for n in sorted(expected):
        bs, br, bage = b_nodes[n]
        as_, ar, aage = a_nodes[n]
        why = []
        if bs != as_:
            why.append(f"status {bs}->{as_}")
        if br != ar:
            why.append(f"release {br}->{ar}")
        if n not in down:
            if aage >= max_age:
                why.append(f"heartbeat frozen hb_age={aage}>={max_age}")
            elif aage - bage > max_jump:
                why.append(f"heartbeat age jump {bage}->{aage}")
        lines.append(f"node {n} before={bs}/{br}/{bage} after={as_}/{ar}/{aage} -> {'CHANGED ' + '; '.join(why) if why else 'same'}")
        if why:
            changed.append(n)
    for port in sorted(ready_ports):
        if b_ready[port] != a_ready[port]:
            lines.append(f"ready:{port} {b_ready[port]}->{a_ready[port]} -> CHANGED")
            changed.append(f"ready:{port}")
    if changed:
        lines.append(f"FLEET_CHANGED {','.join(changed)} (stop; report to the user; never RESUME)")
        return 3, lines
    lines.append(f"FLEET_UNCHANGED nodes={len(expected)} ready_ports={len(ready_ports)} max_hb_age={max_age}")
    return 0, lines


def cmd_fleet_compare(args: argparse.Namespace) -> int:
    rc, lines = fleet_compare(args.before, args.after, args.nodes.split(), args.ready_ports.split(),
                              args.max_hb_age, args.max_hb_jump, (args.known_down or "").split())
    print("\n".join(lines))
    return rc


# Review wac-032-r2 🟡-7: the guard thresholds must match the PRODUCTION node heartbeat
# parameters that site check S-00 reads (interval = runtime/control_plane_session.py
# DEFAULT_HEARTBEAT_INTERVAL_SECONDS, 2.0 in 67b401a; fail-closed timeout = node config
# control_plane.heartbeat_timeout_seconds, 15 in the examples). Rules:
#   max heartbeat age and max age jump within [2, 3] x interval (2.5 x is the target):
#     tighter = a merely late heartbeat reads as frozen; looser = a dead node is seen late;
#   settle window >= timeout + 3 x interval: a fail-closed HALT is written with the first
#     heartbeat after the channel returns, so sampling earlier could miss it.
# The defaults (2 s, 15 s -> 5, 5, 60) pass. Any other set needs the user's confirmation
# (o0-runbook-deploy.md §0); this check only refuses inconsistent sets.
def fleet_params_check(interval: float, timeout: float, max_age: float, max_jump: float, settle: float,
                       samples: int, sample_interval: float) -> tuple[int, list[str]]:
    if not all(v > 0 for v in (interval, timeout, max_age, max_jump)) or settle < 0 or samples < 1 or sample_interval < 0:
        return 2, [f"FLEET_PARAMS_UNCOMPARABLE non-positive value (interval={interval} timeout={timeout} max_age={max_age} "
                   f"max_jump={max_jump} settle={settle} samples={samples} sample_interval={sample_interval})"]
    lo, hi, target = 2 * interval, 3 * interval, 2.5 * interval
    min_settle = timeout + 3 * interval
    why = []
    if not lo <= max_age <= hi:
        why.append(f"O0_FLEET_MAX_HB_AGE={max_age} outside [{lo:g}, {hi:g}] (2-3 x interval {interval:g})")
    if not lo <= max_jump <= hi:
        why.append(f"O0_FLEET_MAX_HB_JUMP={max_jump} outside [{lo:g}, {hi:g}]")
    if settle < min_settle:
        why.append(f"O0_FLEET_SETTLE_S={settle} < timeout {timeout:g} + 3 x interval {interval:g} = {min_settle:g}")
    head = (f"node heartbeat interval={interval:g}s timeout={timeout:g}s; guard max_hb_age={max_age:g} max_hb_jump={max_jump:g} "
            f"settle={settle:g}s samples={samples} every {sample_interval:g}s")
    if why:
        return 2, [head] + [f"  {w}" for w in why] + [
            f"FLEET_PARAMS_INCONSISTENT suggested: O0_FLEET_MAX_HB_AGE={target:g} O0_FLEET_MAX_HB_JUMP={target:g} "
            f"O0_FLEET_SETTLE_S={max(60.0, min_settle):g} (adjust only with the user's confirmation, runbook §0)"]
    return 0, [head, f"FLEET_PARAMS_OK target_max_hb_age={target:g} min_settle={min_settle:g}"]


def cmd_fleet_params(args: argparse.Namespace) -> int:
    rc, lines = fleet_params_check(args.hb_interval_s, args.hb_timeout_s, args.max_hb_age, args.max_hb_jump,
                                   args.settle_s, args.samples, args.sample_interval_s)
    print("\n".join(lines))
    return rc


# ---------------------------------------------------------------- control-plane unit isolation
# Review wac-032-r2 🟡-6 (site check S-10 as a machine gate of stage O): the three control-plane
# units share one code directory (D-04), so the ONLY thing keeping the watcher gateway/snapshot
# values inside operator-query is its env file. node-control and event-ingest must not load
# operator-query.env, must not carry any WATCHER_*TOKEN name in Environment= or in their own
# env files, and operator-query must run from the directory stage O installs into.
# Names only: env VALUES are parsed in-process and never printed.
WATCHER_CRED_NAME = re.compile(r"^WATCHER_[A-Z0-9_]*TOKEN[A-Z0-9_]*$")


# Review wac-072 🟡-3: a value the parser does not understand is UNCOMPARABLE, never silently
# skipped. systemd prints "EnvironmentFiles=<path> (ignore_errors=yes|no)"; the path is cut from
# the RIGHT at " (ignore_errors=" so a path with spaces still parses. A bare "-/path" (no suffix)
# is accepted only without whitespace; anything else (no leading '/', unknown suffix) is kept in
# "unparsed" and the gate returns CP_ISOLATION_UNCOMPARABLE.
ENVFILES_VALUE = re.compile(r"^(-?)(/.*?)(?: \(ignore_errors=(yes|no)\))?$")


def _parse_unit_show(text: str) -> dict:
    out: dict = {"envfiles": [], "env_names": [], "unparsed": []}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        if key == "EnvironmentFiles":
            v = value.strip()
            if not v:
                continue
            m = ENVFILES_VALUE.match(v)
            if not m or (m.group(3) is None and re.search(r"\s", m.group(2))):
                out["unparsed"].append(v)
                continue
            out["envfiles"].append((m.group(2), m.group(1) == "-" or m.group(3) == "yes"))
        elif key == "Environment":
            try:
                words = shlex.split(value)
            except ValueError:
                words = value.split()
            out["env_names"] += [w.split("=", 1)[0] for w in words if "=" in w]
            # the role is not a secret: its VALUE is kept (RS-17: every control-plane unit sets a non-'all' role explicitly)
            out.setdefault("roles", []).extend(w.split("=", 1)[1] for w in words if w.startswith("CONTROL_PLANE_APP_ROLE="))
        elif key == "ExecStart":
            m = re.search(r"argv\[\]=([^;]*)", value)
            out["ExecStart"] = (m.group(1) if m else value).strip()
        elif key in ("LoadState", "WorkingDirectory", "NeedDaemonReload", "FragmentPath", "DropInPaths", "User"):
            out[key] = value.strip()
    return out


def _env_file_names(path: str) -> list[str] | None:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    names = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m:
            names.append(m.group(1))
    return names


def _uvicorn_listen_ok(argv: list[str]) -> bool:
    def opt(name: str) -> str | None:
        return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None
    return bool(argv) and argv[0].endswith("/uvicorn") and "read_api:app" in argv and opt("--host") == "127.0.0.1" and opt("--port") == WGW_PORT


def cp_isolation_check(shows: dict[str, str], oq_unit: str, other_units: list[str], oq_env: str, cp_root: str,
                       wgw_unit: str | None = None, wgw_env: str | None = None, wgw_root: str | None = None,
                       wgw_may_be_absent: bool = False, allow_auth_secret: bool = False) -> tuple[int, list[str]]:
    """S-10 as a gate, WGW-1.0.4 RS-17 (four units). The three existing units (operator-query, node-control, event-ingest):
    loaded, NeedDaemonReload=no, an explicit non-'all' CONTROL_PLANE_APP_ROLE, WorkingDirectory still the shared directory,
    no WATCHER_GATEWAY_TOKEN anywhere in their env files or Environment= (node-control / event-ingest: no WATCHER_*TOKEN name
    at all, and they never load operator-query.env), none of them loads watcher-gateway.env. The watcher-gateway unit
    (``wgw_unit``; absent before stage O when ``wgw_may_be_absent``): the only role watcher-gateway, User
    trader-v3-cp-watcher-gateway, WorkingDirectory under ``wgw_root``, uvicorn on --host 127.0.0.1 --port 8186, exactly one
    EnvironmentFiles= (``wgw_env``) whose names are a subset of the whitelist (no signal token, no AUTH_SECRET_KEY unless
    U-13 (iii) - ``allow_auth_secret``), no CONTROL_PLANE_EXPECT_DATABASE_ROLE / DATABASE_URL. Names only, values never printed."""
    lines: list[str] = []
    three = [oq_unit] + other_units
    wanted = three + ([wgw_unit] if wgw_unit else [])
    units = {u: _parse_unit_show(shows.get(u, "")) for u in wanted}
    wgw_present = bool(wgw_unit)
    evidence = []
    for u, info in units.items():
        if u == wgw_unit and info.get("LoadState") == "not-found" and wgw_may_be_absent:
            wgw_present = False
            lines.append(f"{u}: not installed yet (preflight before stage O)")
            continue
        if info.get("LoadState") != "loaded":
            return 2, [f"CP_ISOLATION_UNCOMPARABLE {u}: LoadState={info.get('LoadState', '<missing>')} (unit unknown or systemctl output unreadable)"]
        # review wac-072 🟡-3: a drop-in changed on disk but not loaded is invisible to `systemctl show`;
        # the next daemon-reload + restart of that unit would load it
        if info.get("NeedDaemonReload") != "no":
            return 2, [f"CP_ISOLATION_UNCOMPARABLE {u}: NeedDaemonReload={info.get('NeedDaemonReload', '<missing>')} (unit files changed on disk "
                       "but not loaded: the gate would judge the OLD configuration; report to the user, do not daemon-reload here)"]
        if info["unparsed"]:
            return 2, [f"CP_ISOLATION_UNCOMPARABLE {u}: {len(info['unparsed'])} EnvironmentFiles= value(s) in an unexpected format "
                       "(not '<absolute path> (ignore_errors=yes|no)'): cannot tell which env files the unit loads"]
        evidence.append(f"{u}: FragmentPath={info.get('FragmentPath') or '<none>'} DropInPaths={info.get('DropInPaths') or '<none>'}")
    violations = []
    oq_env_real = os.path.realpath(oq_env)
    wgw_env_real = os.path.realpath(wgw_env) if wgw_env else None
    root = os.path.normpath(cp_root)

    def env_names(u: str, info: dict) -> tuple[list[str], int | None]:
        names: list[str] = []
        for path, ignore in info["envfiles"]:
            got = _env_file_names(path)
            if got is None:
                if not ignore:
                    return [], 2
                lines.append(f"{u}: env file {path} absent (ignore_errors=yes)")
                continue
            names += got
        return names, None

    for u in three:
        info = units[u]
        wd = os.path.normpath(info.get("WorkingDirectory") or "/")
        if wd not in (root, os.path.join(root, "api")):
            violations.append(f"CODE_DIR_MISMATCH {u} WorkingDirectory={wd} is not {root} or {root}/api (the three units keep the shared "
                              "directory; WGW-1.0.4 never moves them)")
        roles = info.get("roles") or []
        if len(roles) != 1 or roles[0] in ("", "all") or roles[0].replace("_", "-") == "watcher-gateway":
            violations.append(f"ROLE_NOT_EXPLICIT {u} CONTROL_PLANE_APP_ROLE={','.join(roles) or '<unset>'} (RS-17: an explicit non-'all' "
                              "role; a missing role falls back to 'all')")
        for path, _ignore in info["envfiles"]:
            if wgw_env_real and (os.path.realpath(path) == wgw_env_real or os.path.basename(path) == "watcher-gateway.env"):
                violations.append(f"ENVFILE_ISOLATION VIOLATION {u} loads {path} (the watcher-gateway env)")
            if u != oq_unit and (os.path.realpath(path) == oq_env_real or os.path.basename(path) == "operator-query.env"):
                violations.append(f"ENVFILE_ISOLATION VIOLATION {u} loads {path} (operator-query's env)")
        names, err = env_names(u, info)
        if err:
            return 2, [f"CP_ISOLATION_UNCOMPARABLE {u}: an env file without ignore_errors is unreadable"]
        names += info["env_names"]
        bad = sorted({n for n in names if (WATCHER_CRED_NAME.match(n) if u != oq_unit else n.startswith(WGW_GATEWAY_TOKEN))})
        if bad:
            violations.append(f"ENVFILE_ISOLATION VIOLATION {u}: defines {','.join(bad)} (WGW-1.0.4: the gateway token lives only in "
                              "watcher-gateway.env)")
        if not any(v.startswith(("ENVFILE_ISOLATION VIOLATION " + u)) for v in violations):
            lines.append(f"ENVFILE_ISOLATION ok {u} role={roles[0] if roles else '<unset>'} envfiles={len(info['envfiles'])} env_names={len(info['env_names'])}")
    if wgw_unit and wgw_present:
        info = units[wgw_unit]
        wd = os.path.normpath(info.get("WorkingDirectory") or "/")
        rel_root = os.path.normpath(wgw_root or "/nonexistent")
        if not (wd.startswith(rel_root + os.sep) and wd.endswith(os.path.join("services", "control-plane", "api"))):
            violations.append(f"WGW_CODE_DIR {wgw_unit} WorkingDirectory={wd} is not <{rel_root}>/<release-sha>/services/control-plane/api (R23)")
        if (info.get("roles") or []) != ["watcher-gateway"]:
            violations.append(f"WGW_ROLE {wgw_unit} CONTROL_PLANE_APP_ROLE={','.join(info.get('roles') or []) or '<unset>'} (want watcher-gateway)")
        if info.get("User") != WGW_USER:
            violations.append(f"WGW_USER {wgw_unit} User={info.get('User') or '<unset>'} (want {WGW_USER})")
        if not _uvicorn_listen_ok((info.get("ExecStart") or "").split()):
            violations.append(f"WGW_LISTEN {wgw_unit} ExecStart is not 'uvicorn read_api:app --host 127.0.0.1 --port {WGW_PORT}'")
        bad_env = sorted(n for n in info["env_names"] if n in ("CONTROL_PLANE_EXPECT_DATABASE_ROLE", "DATABASE_URL") or WATCHER_CRED_NAME.match(n))
        if bad_env:
            violations.append(f"WGW_ENVIRONMENT {wgw_unit} Environment= defines {','.join(bad_env)}")
        files = [path for path, _ignore in info["envfiles"]]
        if len(files) != 1 or not wgw_env_real or os.path.realpath(files[0]) != wgw_env_real:
            violations.append(f"WGW_ENVFILE {wgw_unit} EnvironmentFiles={files} (want exactly [{wgw_env}])")
        else:
            try:
                env = parse_env_file(Path(files[0]))
            except (OSError, SystemExit):
                return 2, [f"CP_ISOLATION_UNCOMPARABLE {wgw_unit}: its env file is unreadable"]
            probs = _wgw_env_problems(env, allow_auth_secret)
            violations += [f"WGW_ENV {wgw_unit}: {p}" for p in probs]
            if not probs:
                lines.append(f"WGW_ENV ok {wgw_unit} names={','.join(sorted(env))}")
    lines += evidence
    lines.append("working directories: " + " ".join(f"{u}={os.path.normpath(units[u].get('WorkingDirectory') or '<unset>')}" for u in units))
    if violations:
        return 1, lines + violations + [f"CP_ISOLATION_FAILED violations={len(violations)} (stage O blocked; report to the user)"]
    shared = [u for u in three if os.path.normpath(units[u].get("WorkingDirectory") or "/") in (root, os.path.join(root, "api"))]
    return 0, lines + [f"CP_ISOLATION_OK units={len(three) + (1 if wgw_present else 0)} watcher_gateway={'present' if wgw_present else 'absent'} "
                       f"shared_code_dir={','.join(shared) or 'none'} d04=no (WGW-1.0.4: this stage never touches the shared directory)"]


def cmd_cp_isolation(args: argparse.Namespace) -> int:
    import subprocess
    shows = {}
    for u in [args.oq_unit] + args.other_unit + ([args.wgw_unit] if args.wgw_unit else []):
        r = subprocess.run(["systemctl", "show", u, "-p", "LoadState", "-p", "WorkingDirectory", "-p", "EnvironmentFiles", "-p", "Environment",
                            "-p", "NeedDaemonReload", "-p", "FragmentPath", "-p", "DropInPaths", "-p", "ExecStart", "-p", "User"],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"CP_ISOLATION_UNCOMPARABLE systemctl show {u} rc={r.returncode}")
            return 2
        shows[u] = r.stdout
    rc, lines = cp_isolation_check(shows, args.oq_unit, args.other_unit, args.oq_env, args.cp_root, args.wgw_unit, args.wgw_env,
                                   args.wgw_root, args.wgw_may_be_absent, args.allow_auth_secret_key)
    print("\n".join(lines))
    return rc


# ---------------------------------------------------------------- WGW-1.0.4 stage O (watcher-gateway, R23; RS-16/17/19)
# Contract: contracts/backend-api.md §9.14.6 (new role, independent code dir, deploy steps, rollback, PC-6, U-13, V-5).
# Nothing here writes outside --out / --snapshot-out, and no value of a secret is ever printed (names only).
WGW_UNIT = "trader-v3-controlplane-watcher-gateway.service"
WGW_USER = "trader-v3-cp-watcher-gateway"
WGW_PORT = "8186"
WGW_READER_TOKENS = ("RISK_ADMIN_TOKEN", "VIEWER_TOKEN", "REVIEWER_TOKEN", "SYSTEM_OBSERVER_TOKEN")
WGW_GATEWAY_TOKEN = "WATCHER_GATEWAY_TOKEN"
WGW_OPTIONAL = ("WATCHER_GATEWAY_URL", "WATCHER_GATEWAY_CONFIG_SLOTS", "WATCHER_GATEWAY_MEDIA_SLOTS")
WGW_AUTH_SECRET = "AUTH_SECRET_KEY"      # only with U-13 (iii): PC-6 (v) and "the app needs session tokens"
# §9.14.6 "不得写入" plus the default-not-written signal tokens (G-36)
WGW_FORBIDDEN = ("DATABASE_URL", "WATCHER_SNAPSHOT_TOKEN", "NAUTILUS_NODE_TOKEN", "NAUTILUS_NODE_AUTH_JSON", "CONTROL_PLANE_AUTH_SECRET",
                 "CONTROL_PLANE_EXPECT_DATABASE_ROLE", "WATCHER_BROWSER_PROXY_TOKEN", "SIGNAL_TOKEN_ACCOUNT_A", "SIGNAL_TOKEN_ACCOUNT_B",
                 "SIGNAL_TOKEN_ACCOUNT_C", "SIGNAL_TOKEN_ACCOUNT_D")
WGW_FORBIDDEN_RE = re.compile(r"^(SIGNAL_TOKEN_|BINANCE_|TELEGRAM_|TG_|EXCHANGE_|API_SECRET|API_KEY)")
CONTRACT_TOKEN_RE = re.compile(r"^[\x21-\x7E]{32,}$")
SHARED_SUBDIRS = ("services/control-plane", "services/nautilus-node/observability", "packages", "db/migrations")
SNAPSHOT_SKIP_DIRS = ("__pycache__",)


def wgw_whitelist(with_auth_secret: bool) -> tuple[str, ...]:
    return (WGW_GATEWAY_TOKEN,) + WGW_READER_TOKENS + WGW_OPTIONAL + ((WGW_AUTH_SECRET,) if with_auth_secret else ())


def _wgw_env_problems(env: dict[str, str], with_auth_secret: bool) -> list[str]:
    """Names only. The watcher-gateway env: names within the whitelist, no forbidden name, the gateway token and the four
    reader tokens configured in the contract format and pairwise distinct (§9.2 E-02; the in-process check sees only these)."""
    problems = []
    allowed = set(wgw_whitelist(with_auth_secret))
    for name in sorted(env):
        if name in WGW_FORBIDDEN or WGW_FORBIDDEN_RE.match(name) or (name == WGW_AUTH_SECRET and not with_auth_secret):
            problems.append(f"forbidden name {name} (§9.14.6 env: never / not by default)")
        elif name not in allowed:
            problems.append(f"name {name} is not on the watcher-gateway whitelist")
    digests: dict[str, list[str]] = {}
    for name in (WGW_GATEWAY_TOKEN,) + WGW_READER_TOKENS:
        value = (env.get(name) or "").strip()
        if not value:
            problems.append(f"{name} not configured")
            continue
        if not CONTRACT_TOKEN_RE.match(value):
            problems.append(f"{name} violates the contract format ^[\\x21-\\x7E]{{32,}}$")
        digests.setdefault(hashlib.sha256(value.encode()).hexdigest(), []).append(name)
    for names in digests.values():
        if len(names) > 1:
            problems.append("values not distinct: " + ",".join(sorted(names)))
    if with_auth_secret and not (env.get(WGW_AUTH_SECRET) or "").strip():
        problems.append(f"{WGW_AUTH_SECRET} requested (U-13 (iii)) but not configured")
    return problems


def cmd_wgw_env(args: argparse.Namespace) -> int:
    """Build (or --check) the watcher-gateway env file from the live operator-query env (the four reader tokens and, only
    with --with-auth-secret-key, AUTH_SECRET_KEY) and the credential set's watcher-gateway.env (the gateway token)."""
    if args.check:
        env = parse_env_file(args.check)
        problems = _wgw_env_problems(env, args.with_auth_secret_key)
        mode = oct(args.check.stat().st_mode & 0o777)
        if mode != "0o600":
            problems.append(f"file mode {mode} (want 0600)")
        for p in problems:
            print(f"FAIL {p}")
        print(f"WGW_ENV_{'FAILED' if problems else 'OK'} names={','.join(sorted(env))}")
        return 1 if problems else 0
    oq = parse_env_file(args.oq_env)
    frag = parse_env_file(args.gateway_fragment)
    items: list[tuple[str, str]] = []
    if WGW_GATEWAY_TOKEN in frag:
        items.append((WGW_GATEWAY_TOKEN, frag[WGW_GATEWAY_TOKEN]))
    for name in WGW_READER_TOKENS:
        if name in oq:
            items.append((name, oq[name]))
    for kv in args.set_optional or []:
        name, _, value = kv.partition("=")
        if name not in WGW_OPTIONAL or not re.fullmatch(r"[A-Za-z0-9:/._-]{1,200}", value):
            print(f"WGW_ENV_REFUSED --set-optional {name}: only {','.join(WGW_OPTIONAL)} with a plain value")
            return 1
        items.append((name, value))
    if args.with_auth_secret_key:
        if WGW_AUTH_SECRET not in oq:
            print(f"WGW_ENV_REFUSED {WGW_AUTH_SECRET} is not in the operator-query env (PC-6 (v) says no: U-13 (iii) cannot hold)")
            return 1
        items.append((WGW_AUTH_SECRET, oq[WGW_AUTH_SECRET]))
    problems = _wgw_env_problems(dict(items), args.with_auth_secret_key)
    extra = sorted(set(frag) - {WGW_GATEWAY_TOKEN})
    if extra:
        problems.append(f"the credential fragment carries other names too: {','.join(extra)}")
    if problems:
        for p in problems:
            print(f"FAIL {p}")
        print("WGW_ENV_FAILED nothing written")
        return 1
    if args.out.exists() or args.out.is_symlink():
        print(f"WGW_ENV_REFUSED {args.out} already exists (never overwritten here)")
        return 1
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("".join(f"{k}={v}\n" for k, v in items))
    print(f"WGW_ENV_WRITTEN names={','.join(k for k, _ in items)} mode=0600 file={args.out.name}")
    return 0


def _tree_snapshot(root: Path, subdirs: tuple[str, ...]) -> dict[str, list]:
    """rel -> [kind, sha256 | link target, mode, uid, gid] for every entry under the sub-directories (nothing skipped)."""
    out: dict[str, list] = {}
    for sub in subdirs:
        base = root / sub
        if not base.exists():
            out[sub] = ["absent", "", 0, 0, 0]
            continue
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames.sort()
            d = Path(dirpath)
            st = d.lstat()
            out[str(d.relative_to(root))] = ["dir", "", st.st_mode & 0o7777, st.st_uid, st.st_gid]
            for name in sorted(filenames) + [n for n in dirnames if (d / n).is_symlink()]:
                f = d / name
                st = f.lstat()
                rel = str(f.relative_to(root))
                if f.is_symlink():
                    out[rel] = ["link", os.readlink(f), st.st_mode & 0o7777, st.st_uid, st.st_gid]
                elif f.is_file():
                    out[rel] = ["file", sha256_file(f), st.st_mode & 0o7777, st.st_uid, st.st_gid]
                else:
                    out[rel] = ["other", "", st.st_mode & 0o7777, st.st_uid, st.st_gid]
    return out


def shared_tree_check(root: Path, manifest_text: str, subdirs: tuple[str, ...], accept: str | None) -> tuple[int, list[str], str]:
    """(rc, lines, drift_sha256). The shared code directory against the 67b401a manifest (§9.14.6 部署前只读核对; task wac-105:
    on drift list the files and the options, hand it to the user, never fix or overwrite)."""
    lines: list[str] = []
    entries = []
    for raw in manifest_text.splitlines():
        if raw.strip():
            sha, rel = raw.split("  ", 1)
            entries.append((sha, rel))
    if not entries:
        return 2, ["MANIFEST_UNCOMPARABLE cp-shared-vs-67b401a compared=0"], ""
    drift = []
    for sha, rel in entries:
        f = root / rel
        if f.is_symlink() or not f.is_file():
            drift.append(f"MISSING {rel}")
        elif sha256_file(f) != sha:
            drift.append(f"MODIFIED {rel}")
    known = {rel for _sha, rel in entries}
    extra = []
    for sub in subdirs:
        base = root / sub
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(n for n in dirnames if n not in SNAPSHOT_SKIP_DIRS)
            for name in sorted(filenames):
                rel = str((Path(dirpath) / name).relative_to(root))
                if rel not in known and not name.endswith(".pyc"):
                    extra.append(rel)
    drift_text = "\n".join(sorted(drift))
    drift_sha = hashlib.sha256(drift_text.encode()).hexdigest() if drift else ""
    for rel in extra[:50]:
        lines.append(f"INFO EXTRA {rel} (not tracked at 67b401a; this stage does not load it, the new unit runs from its own directory)")
    if not drift:
        lines.append(f"MANIFEST_OK cp-shared-vs-67b401a compared={len(entries)} extra_untracked={len(extra)}")
        return 0, lines, ""
    lines += [f"DRIFT {d}" for d in sorted(drift)[:200]]
    if accept and accept == drift_sha:
        lines.append(f"MANIFEST_DRIFT_ACCEPTED cp-shared-vs-67b401a drift={len(drift)} drift_sha256={drift_sha} (the user's decision for this "
                     "stage; the before/after snapshot of the shared directory stays a hard gate)")
        return 0, lines, drift_sha
    lines += [
        f"MANIFEST_DRIFT cp-shared-vs-67b401a compared={len(entries)} drift={len(drift)} drift_sha256={drift_sha}",
        "SUGGESTION stop here: nothing was changed. This stage neither reads nor writes these files at run time: watcher-gateway runs "
        "from its own directory (R23) and the three running units keep what they loaded.",
        "SUGGESTION option A (user decision): accept exactly this drift for this stage - re-run preflight with --accept-shared-drift "
        f"{drift_sha} (bound to this list; any other difference still stops); before/after identity of the shared directory stays a gate.",
        "SUGGESTION option B (user decision): align the shared directory first, in a separately authorized control-plane task (production "
        "has had hot-mounted files outside git before), then re-run this preflight.",
        "NEVER here: fix, restore, overwrite or copy any file of the shared directory.",
    ]
    return 1, lines, drift_sha


def cmd_shared_tree(args: argparse.Namespace) -> int:
    subdirs = tuple(args.sub or SHARED_SUBDIRS)
    if args.compare:
        before = json.loads(args.compare.read_text(encoding="utf-8"))
        now = _tree_snapshot(args.root, subdirs)
        changed = sorted(k for k in before.keys() & now.keys() if before[k] != now[k])
        added, removed = sorted(now.keys() - before.keys()), sorted(before.keys() - now.keys())
        if not before:
            print("SHARED_UNCOMPARABLE the earlier snapshot is empty")
            return 2
        if changed or added or removed:
            for k in (changed + added + removed)[:50]:
                print(f"CHANGED {k}")
            print(f"SHARED_CHANGED changed={len(changed)} added={len(added)} removed={len(removed)} (stop, report to the user; never repair here)")
            return 1
        print(f"SHARED_UNCHANGED entries={len(now)} (identical to {args.compare.name})")
        return 0
    rc, lines, _sha = shared_tree_check(args.root, args.manifest.read_text(encoding="utf-8"), subdirs, args.accept_drift_sha256)
    print("\n".join(lines))
    if args.snapshot_out and rc == 0:
        fd = os.open(args.snapshot_out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(_tree_snapshot(args.root, subdirs), handle, sort_keys=True)
        print(f"SHARED_SNAPSHOT_WRITTEN {args.snapshot_out.name}")
    return rc


def daemon_reload_check(show_text: str, exclude: list[str], require: list[str]) -> tuple[int, str]:
    """`systemctl show -p Id -p NeedDaemonReload <every unit>` -> (rc, line). Any yes = DAEMON_RELOAD_PENDING (wac-060: a
    daemon-reload acts on the whole host and would load every pending change; review wac-096 r5 🟢-4: the user decides)."""
    units: dict[str, str] = {}
    cur: dict[str, str] = {}
    for raw in show_text.splitlines() + [""]:
        if not raw.strip():
            if cur.get("Id"):
                units[cur["Id"]] = cur.get("NeedDaemonReload", "<missing>")
            cur = {}
            continue
        k, _, v = raw.partition("=")
        cur[k.strip()] = v.strip()
    if not units:
        return 2, "DAEMON_RELOAD_UNCOMPARABLE units=0 (systemctl output empty or unreadable)"
    missing = [u for u in require if u not in units]
    if missing:
        return 2, f"DAEMON_RELOAD_UNCOMPARABLE required unit(s) absent from the list: {','.join(missing)}"
    bad = sorted(u for u, v in units.items() if u not in exclude and v != "no")
    if bad:
        return 1, (f"DAEMON_RELOAD_PENDING units={','.join(bad[:20])} count={len(bad)} (stop: no daemon-reload here; report the list to the "
                   "user, who decides whether to authorize one reload separately)")
    return 0, f"DAEMON_RELOAD_CLEAN units={len(units)} excluded={','.join(exclude) or '-'}"


def cmd_daemon_reload_check(args: argparse.Namespace) -> int:
    rc, line = daemon_reload_check(args.show.read_text(encoding="utf-8"), args.exclude or [], args.require or [])
    print(line)
    return rc


def port_check(ss_text: str, port: str, expect: str, record: tuple[int, int] | None) -> tuple[int, list[str]]:
    """`ss -H -ltnp` output: expect 'free' (nothing listens on the port) or 'loopback-only' (>= 1 listener, all 127.0.0.1)."""
    lines, listeners = [], []
    for raw in ss_text.splitlines():
        cols = raw.split()
        if len(cols) < 4:
            continue
        local = cols[3]
        host, _, p = local.rpartition(":")
        if not p.isdigit():
            continue
        if p == port:
            listeners.append(host)
        if record and record[0] <= int(p) <= record[1]:
            proc = re.search(r'users:\(\("([^"]{1,64})"', raw)
            lines.append(f"RECORD port {p} local={host} process={proc.group(1) if proc else '?'}")
    if expect == "free":
        ok = not listeners
        lines.append(f"PORT_{'FREE' if ok else 'IN_USE'} {port} listeners={len(listeners)}"
                     + ("" if ok else " (stop: the contract port is taken; Planner recalls the Architect, O-0 never picks another port)"))
    else:
        ok = bool(listeners) and all(h in ("127.0.0.1",) for h in listeners)
        lines.append(f"PORT_{'LOOPBACK_ONLY' if ok else 'NOT_LOOPBACK_ONLY'} {port} listeners={','.join(listeners) or 'none'}")
    return (0 if ok else 1), lines


def cmd_port_check(args: argparse.Namespace) -> int:
    rec = tuple(int(x) for x in args.record_range.split("-")) if args.record_range else None
    rc, lines = port_check(args.ss_file.read_text(encoding="utf-8"), args.port, args.expect, rec)  # type: ignore[arg-type]
    print("\n".join(lines))
    return rc


# V-5 (§9.14.6, RS-19): the closed expectation set, status codes only
V5_REQUESTS = (("/v1/watcher/status", "panel"), ("/v1/watcher/dialogs", "panel"), ("/V1/WATCHER/status", "panel"),
               ("/m/v1/watcher/status", "mobile-gateway"), ("/m/v1/accounts", "mobile-control"))


def v5_expected(kind: str, phase: str) -> tuple[int, ...]:
    if kind == "panel":
        return (404,)
    if kind == "mobile-control":
        return (401,)
    return (401, 403) if phase == "unit-running" else (502,)


def cmd_public_direct_check(args: argparse.Namespace) -> int:
    """V-5. External mode (evidence for verify): run OUTSIDE jp-24, real DNS, no proxy (http.client ignores *_PROXY), no
    --resolve, no credentials, no cookies; prints status, content_type and size only (the body is read and dropped).
    --loopback: the jp-24 supplementary check (connects to 127.0.0.1 with the public name as SNI and Host); its output is
    never accepted as external evidence (mode=loopback)."""
    import socket
    import ssl
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", args.host):
        print("V5_PUBLIC_CHECK_FAILED --host is not a plain host name")
        return 1
    mode = "loopback" if args.loopback else ("external" if args.scheme == "https" and args.port == 443 else "test")
    fails = 0
    for path, kind in V5_REQUESTS:
        want = v5_expected(kind, args.phase)
        try:
            if args.scheme == "https":
                ctx = ssl.create_default_context()
                conn = http.client.HTTPSConnection(args.host, args.port, timeout=args.timeout, context=ctx)
                if args.loopback:
                    raw = socket.create_connection(("127.0.0.1", args.port), timeout=args.timeout)
                    conn.sock = ctx.wrap_socket(raw, server_hostname=args.host)
            else:
                conn = http.client.HTTPConnection("127.0.0.1" if args.loopback else args.host, args.port, timeout=args.timeout)
            conn.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", args.host)
            conn.putheader("User-Agent", "o0-v5-check")
            conn.endheaders()
            resp = conn.getresponse()
            size = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > 1 << 20:
                    break
            status, ctype = resp.status, (resp.getheader("Content-Type") or "").split(";")[0][:40]
            conn.close()
        except (OSError, http.client.HTTPException) as exc:
            status, ctype, size = 0, "", 0
            print(f"V5 GET {path} error={type(exc).__name__}")
        ok = status in want
        fails += 0 if ok else 1
        print(f"V5 GET {path} status={status} content_type={ctype or '-'} size={size} want={'|'.join(map(str, want))} {'ok' if ok else 'FAIL'}")
    now = int(time.time())
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    tail = f"mode={mode} phase={args.phase} host={args.host} at={stamp} epoch={now} checks={len(V5_REQUESTS)}"
    if fails:
        print(f"V5_PUBLIC_CHECK_FAILED {tail} failed={fails}")
        return 1
    print(f"V5_PUBLIC_CHECK_OK {tail}")
    return 0


def v5_evidence_check(text: str, host: str, phase: str, not_before: int, max_age_s: int, now: float | None = None) -> tuple[bool, str]:
    rows = [l for l in text.splitlines() if l.startswith("V5 GET ")]
    last = next((l for l in reversed(text.splitlines()) if l.startswith(("V5_PUBLIC_CHECK_OK", "V5_PUBLIC_CHECK_FAILED"))), "")
    m = re.fullmatch(r"V5_PUBLIC_CHECK_OK mode=(\S+) phase=(\S+) host=(\S+) at=\S+ epoch=([0-9]+) checks=([0-9]+)", last)
    if not m:
        return False, "no V5_PUBLIC_CHECK_OK line (failed or not an evidence file)"
    ev_mode, ev_phase, ev_host, epoch, checks = m.group(1), m.group(2), m.group(3), int(m.group(4)), int(m.group(5))
    if ev_mode != "external":
        return False, f"mode={ev_mode}: only an external run (outside jp-24, real DNS, no proxy) is evidence"
    if ev_phase != phase or ev_host != host:
        return False, f"evidence is for phase={ev_phase} host={ev_host}, want phase={phase} host={host}"
    ok_rows = [l for l in rows if l.endswith(" ok")]
    if checks != len(V5_REQUESTS) or len(rows) != len(V5_REQUESTS) or len(ok_rows) != len(V5_REQUESTS):
        return False, f"{len(ok_rows)}/{len(V5_REQUESTS)} requests ok in the evidence"
    t = now if now is not None else time.time()
    if epoch < not_before:
        return False, f"evidence epoch {epoch} is older than the unit start {not_before} (run the external check after apply)"
    if t - epoch > max_age_s:
        return False, f"evidence is {int(t - epoch)} s old (max {max_age_s})"
    return True, f"V5_EVIDENCE_OK host={host} phase={phase} epoch={epoch} age_s={int(t - epoch)}"


def cmd_v5_evidence_check(args: argparse.Namespace) -> int:
    if not args.evidence or not args.evidence.is_file():
        print("DIRECT_GUARD_UNVERIFIED no external V-5 evidence file (verify is NOT complete; never report the rollout as done)")
        return 1
    ok, msg = v5_evidence_check(args.evidence.read_text(encoding="utf-8"), args.host, args.phase, args.not_before, args.max_age_s)
    print(msg if ok else f"DIRECT_GUARD_UNVERIFIED {msg} (verify is NOT complete)")
    return 0 if ok else 1


def render_wgw_unit(trader_root: str, release_sha: str, resource_conf: str) -> str:
    """The watcher-gateway unit (§9.14.6 单元; same shape as jp24-p1-control-plane.sh write_role_unit, without the database
    parts): own user, own code dir (R23), own env file, uvicorn on 127.0.0.1:8186, single worker, no pyc writes; the
    reader resource limits appended like the p1 script does (the conf without its first '[Service]' line)."""
    if not re.fullmatch(r"[0-9a-f]{40}", release_sha):
        raise SystemExit("wgw unit: release sha must be 40 hex characters")
    body = resource_conf.splitlines()
    if not body or body[0].strip() != "[Service]":
        raise SystemExit("wgw unit: the resource conf must start with [Service]")
    wd = f"{trader_root}/releases/watcher-gateway/{release_sha}/services/control-plane/api"
    head = ["[Unit]", "Description=Trader v3 control-plane role watcher-gateway (WGW-1.0.4: own code dir, no database)",
            "After=network-online.target", "Wants=network-online.target", "", "[Service]",
            f"User={WGW_USER}", f"Group={WGW_USER}", f"WorkingDirectory={wd}",
            f"EnvironmentFile={trader_root}/secrets/control-plane/watcher-gateway.env",
            "Environment=CONTROL_PLANE_APP_ROLE=watcher-gateway", "Environment=PYTHONDONTWRITEBYTECODE=1",
            f"ExecStart={trader_root}/.venv-cp/bin/uvicorn read_api:app --host 127.0.0.1 --port {WGW_PORT}", "TimeoutStopSec=15"]
    return "\n".join(head + body[1:] + ["", "[Install]", "WantedBy=multi-user.target"]) + "\n"


def wgw_unit_lint(text: str, trader_root: str, release_sha: str) -> list[str]:
    """Problems of a watcher-gateway unit file (package gate G12 and stage O preflight)."""
    problems = []
    want = {
        "User": WGW_USER, "Group": WGW_USER,
        "WorkingDirectory": f"{trader_root}/releases/watcher-gateway/{release_sha}/services/control-plane/api",
        "EnvironmentFile": f"{trader_root}/secrets/control-plane/watcher-gateway.env",
        "ExecStart": f"{trader_root}/.venv-cp/bin/uvicorn read_api:app --host 127.0.0.1 --port {WGW_PORT}",
    }
    seen: dict[str, list[str]] = {}
    for raw in text.splitlines():
        k, sep, v = raw.partition("=")
        if sep and not raw.startswith(("#", "[")):
            seen.setdefault(k.strip(), []).append(v.strip())
    for k, v in want.items():
        if seen.get(k) != [v]:
            problems.append(f"{k} must be exactly {v!r} (got {seen.get(k)})")
    envs = seen.get("Environment", [])
    if sorted(envs) != sorted(["CONTROL_PLANE_APP_ROLE=watcher-gateway", "PYTHONDONTWRITEBYTECODE=1"]):
        problems.append(f"Environment= must be exactly CONTROL_PLANE_APP_ROLE=watcher-gateway and PYTHONDONTWRITEBYTECODE=1 (got {len(envs)} line(s))")
    for bad in ("ExecStartPre", "ExecStartPost", "ExecReload"):
        if bad in seen:
            problems.append(f"{bad} present (no pg_isready or other pre/post command: this role has no database)")
    if "--workers" in text or "DATABASE" in text or "EXPECT_DATABASE_ROLE" in text:
        problems.append("a worker count or a database setting in the unit")
    if "0.0.0.0" in text or "--host ::" in text:
        problems.append("a non-loopback listener")
    return problems


def cmd_wgw_unit(args: argparse.Namespace) -> int:
    if args.render:
        text = render_wgw_unit(args.trader_root, args.release_sha, args.resource_conf.read_text(encoding="utf-8"))
        args.out.write_text(text, encoding="utf-8")
        print(f"WGW_UNIT_RENDERED {args.out.name} release_sha={args.release_sha}")
        return 0
    problems = wgw_unit_lint(args.lint.read_text(encoding="utf-8"), args.trader_root, args.release_sha)
    for p in problems:
        print(f"FAIL {p}")
    print(f"WGW_UNIT_{'LINT_FAILED' if problems else 'LINT_OK'} {args.lint.name}")
    return 1 if problems else 0


# RS-16 + review wac-096 r5 🟡-C (task wac-105 item 3): THE dependency check. `python -B` (no .pyc anywhere), `env -i` plus the
# whitelisted env, CONTROL_PLANE_APP_ROLE=watcher-gateway, the code dir's services/control-plane/api as the working directory;
# import read_api, build create_app("watcher-gateway"): gateway routes > 0 and ready; an audit hook proves nothing was opened
# or imported from $TRADER_ROOT outside the code dir and the venv (R23 路径约束); the venv and the code dir are unchanged
# afterwards (metadata snapshot). ImportError -> DEPENDENCY_MISSING <module> (stop; never pip anything: Planner decides).
WGW_SMOKE = r'''
import os, sys
code_root, venv_root, trader_root = sys.argv[1], sys.argv[2], sys.argv[3]
allowed = tuple(os.path.realpath(p) for p in (code_root, venv_root, sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix))
os_allow = ("/etc/", "/usr/share/zoneinfo/", "/dev/", "/proc/", "/sys/", "/usr/lib/ssl/", "/usr/lib/locale/", "/usr/share/locale/", "/run/",
            "/System/Library/", "/private/etc/", "/private/var/db/timezone/", "/usr/share/ca-certificates/", "/usr/lib/python3/dist-packages/")
seen = set()
def hook(event, args):
    if event == "open" and args and isinstance(args[0], (str, bytes)):
        p = args[0].decode() if isinstance(args[0], bytes) else args[0]
        seen.add(os.path.realpath(p if os.path.isabs(p) else os.path.join(os.getcwd(), p)))
    elif event == "import" and len(args) > 1 and isinstance(args[1], str):
        seen.add(os.path.realpath(args[1]))
sys.addaudithook(hook)
try:
    import read_api
except ImportError as exc:
    print("DEPENDENCY_MISSING", getattr(exc, "name", None) or type(exc).__name__)
    sys.exit(3)
import watcher_gateway
app = read_api.create_app("watcher-gateway")
names = [getattr(r, "name", "") for r in app.routes if getattr(r, "name", "").startswith("watcher_gateway__")]
paths = {getattr(r, "path", "") for r in app.routes}
ready = watcher_gateway.readiness()[0]
tr = os.path.realpath(trader_root) + os.sep
inside = [p for p in seen if p.startswith(tr) and not p.startswith(allowed)]
outside = [p for p in seen if p.startswith("/") and not p.startswith(allowed) and not p.startswith(os_allow)]
bad_path = [p for p in sys.path if p and os.path.realpath(p).startswith(tr) and not os.path.realpath(p).startswith(allowed)]
problems = []
if not names: problems.append("0 gateway routes")
if not ready: problems.append("gateway not ready (disabled state or tokens missing; reason only in the log)")
if "/v1/accounts" in paths or "/v1/operator/orders" in paths: problems.append("trading routes on the watcher-gateway app")
if inside: problems.append("opened/imported under TRADER_ROOT outside the code dir and the venv: " + ",".join(sorted(inside)[:5]))
if outside: problems.append("opened outside the code dir, the venv, the standard library and the OS allowlist: " + ",".join(sorted(outside)[:5]))
if bad_path: problems.append("sys.path entries under TRADER_ROOT outside the code dir and the venv: " + ",".join(bad_path[:5]))
print("SMOKE gateway_routes=%d ready=%s files_seen=%d" % (len(names), ready, len(seen)))
for p in problems:
    print("SMOKE_PROBLEM " + p)
sys.exit(1 if problems else 0)
'''


def _meta_snapshot(root: Path) -> str:
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames) + sorted(dirnames):
            f = Path(dirpath) / name
            try:
                st = f.lstat()
            except OSError:
                continue
            h.update(f"{f.relative_to(root)} {st.st_size} {st.st_mtime_ns} {st.st_mode}\n".encode())
    return h.hexdigest()


def cmd_wgw_smoke(args: argparse.Namespace) -> int:
    import subprocess
    api = args.code_dir / "services" / "control-plane" / "api"
    venv_py = args.venv / "bin" / "python"
    if not api.is_dir() or not venv_py.exists():
        print(f"SMOKE_FAILED code dir or venv python missing ({api.is_dir()}, {venv_py.exists()})")
        return 2
    env_vals = parse_env_file(args.env_file)
    problems = _wgw_env_problems(env_vals, args.with_auth_secret_key)
    if problems:
        print("SMOKE_FAILED the env file is not a valid watcher-gateway env (run wgw-env --check)")
        return 2
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "CONTROL_PLANE_APP_ROLE": "watcher-gateway", "PYTHONDONTWRITEBYTECODE": "1", **env_vals}
    cmd = [str(venv_py), "-B", "-c", WGW_SMOKE, str(args.code_dir.resolve()), str(args.venv.resolve()), str(args.trader_root)]
    if args.as_user:
        import shutil
        setpriv = shutil.which("setpriv")      # resolved on the caller's PATH (the child's PATH is minimal)
        if not setpriv:
            print("SMOKE_FAILED setpriv (util-linux) not found: cannot run as the unit user")
            return 2
        cmd = [setpriv, f"--reuid={args.as_user}", f"--regid={args.as_user}", "--clear-groups", "--"] + cmd
    before_v, before_c = _meta_snapshot(args.venv), _meta_snapshot(args.code_dir)
    # values reach the child through its environment only: never on a command line, never printed
    r = subprocess.run(cmd, cwd=api, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=args.timeout)
    out = [l for l in r.stdout.splitlines() if l.startswith(("SMOKE", "DEPENDENCY_MISSING"))]
    print("\n".join(out))
    tokens = [v for v in env_vals.values() if len(v) >= 8]
    if any(t in r.stderr for t in tokens):
        print("SMOKE_FAILED the child's stderr contained an env value (not printed)")
        return 1
    after_v, after_c = _meta_snapshot(args.venv), _meta_snapshot(args.code_dir)
    who = args.as_user or "current user (root in preflight, before the unit user exists)"
    if r.returncode == 3:
        print(f"DEPENDENCY_MISSING stop: the shared venv lacks a module the new code imports; never pip into {args.venv} - Planner decides "
              "(optional: a separate venv inside the release dir)")
        return 3
    if after_v != before_v or after_c != before_c:
        print(f"SMOKE_FAILED the import wrote into the {'venv' if after_v != before_v else 'code dir'} (must be byte-for-byte read-only)")
        return 1
    if r.returncode != 0:
        err = (r.stderr.strip().splitlines() or [""])[-1]
        print(f"SMOKE_FAILED rc={r.returncode} as={who} last_error_type={err.split(':')[0][:80]}")
        return 1
    print(f"SMOKE_OK as={who} python=-B venv_unchanged=yes code_dir_unchanged=yes")
    return 0


# ---------------------------------------------------------------- gates
def _bundle_identity(bundle: Path) -> dict:
    release = json.loads((bundle / "RELEASE.json").read_text(encoding="utf-8"))
    return {
        "candidate": release.get("candidate"),
        "deploy_candidate": release.get("deploy_candidate") is True,
        "release_json_sha256": sha256_file(bundle / "RELEASE.json"),
        "sha256sums_sha256": sha256_file(bundle / "SHA256SUMS"),
    }


def _kv(items: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise SystemExit(f"expected key=value, got {item!r}")
        out[key] = value
    return out


def cmd_gate_write(args: argparse.Namespace) -> int:
    ident = _bundle_identity(args.bundle)
    if not ident["deploy_candidate"]:
        print("GATE_WRITE_REFUSED RELEASE.json deploy_candidate is not true")
        return 1
    fields = _kv(args.field)
    for key, path in _kv(args.file_sha).items():
        target = Path(path)
        if not target.is_file():
            print(f"GATE_WRITE_REFUSED {key}: not a file")
            return 1
        fields[key] = sha256_file(target)
    record = {"schema": "o0-gate/v1", "stage": args.stage, "ok": True, "created_epoch": int(time.time()), **ident, "fields": fields}
    fd, tmp = tempfile.mkstemp(prefix=".gate-", dir=str(args.out.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=1, sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, args.out)
    print(f"GATE_WRITTEN stage={args.stage} candidate={ident['candidate']} fields={','.join(sorted(fields))}")
    return 0


def gate_check(gate: Path, stage: str, bundle: Path, max_age_s: int, expect: dict[str, str], now: float | None = None) -> tuple[bool, str]:
    if not gate.is_file():
        return False, f"gate file missing: {gate.name} (run the gate phase first; it writes this file only when every check passed)"
    try:
        record = json.loads(gate.read_text(encoding="utf-8"))
        ident = _bundle_identity(bundle)
    except (OSError, ValueError) as exc:
        return False, f"unreadable gate or bundle ({type(exc).__name__})"
    if record.get("schema") != "o0-gate/v1" or record.get("ok") is not True:
        return False, "gate record is not a passed o0-gate/v1 record"
    if record.get("stage") != stage:
        return False, f"gate stage {record.get('stage')!r} != {stage!r}"
    if not ident["deploy_candidate"]:
        return False, "bundle RELEASE.json deploy_candidate is not true"
    for key in ("candidate", "release_json_sha256", "sha256sums_sha256"):
        if not record.get(key) or record.get(key) != ident[key]:
            return False, f"{key} differs between the gate and the bundle in hand (different candidate or modified bundle)"
    age = (now if now is not None else time.time()) - int(record.get("created_epoch", 0))
    if age < 0 or age > max_age_s:
        return False, f"gate is {int(age)}s old (max {max_age_s}s); re-run the gate phase"
    fields = record.get("fields") or {}
    for key, value in expect.items():
        if key not in fields:
            return False, f"gate lacks field {key}"
        if fields[key] != value:
            return False, f"gate field {key} differs from the value in hand"
    return True, f"GATE_OK stage={stage} candidate={ident['candidate']} age_s={int(age)} fields_checked={len(expect)}"


def cmd_gate_check(args: argparse.Namespace) -> int:
    if not args.gate.is_file():
        print(f"GATE_FAIL gate file missing: {args.gate.name} (run the gate phase first; it writes this file only when every check passed)")
        return 1
    expect = _kv(args.expect)
    for key, path in _kv(args.expect_file_sha).items():
        target = Path(path)
        if not target.is_file():
            print(f"GATE_FAIL {key}: {target.name} is not a file")
            return 1
        expect[key] = sha256_file(target)
    ok, msg = gate_check(args.gate, args.stage, args.bundle, args.max_age_s, expect)
    print(msg if ok else f"GATE_FAIL {msg}")
    return 0 if ok else 1


# ---------------------------------------------------------------- warm-up evidence
WARMUP_RE = re.compile(r"snapshot_warmup result=(\S+) revision=(\S+) content_sha256=(\S+) pid=([0-9]+) role=operator-query duration_ms=[0-9]+")


def warmup_check(text: str, workers: int, min_revision: int, r0_prefix: str | None) -> tuple[bool, list[str]]:
    rows = [m.groups() for m in WARMUP_RE.finditer(text)]
    if not rows:
        return False, ["WARMUP_UNCOMPARABLE 0 snapshot_warmup lines (is wac-041 deployed? right unit and --since?)"]
    by_pid: dict[str, list[tuple]] = {}
    for row in rows:
        by_pid.setdefault(row[3], []).append(row)
    out = []
    bad = []
    for pid, items in sorted(by_pid.items()):
        result, revision, prefix, _ = items[-1]
        out.append(f"pid={pid} result={result} revision={revision} content_sha256={prefix}")
        if len(items) > 1:
            bad.append(f"pid {pid} warmed up {len(items)} times")
        if result != "success":
            bad.append(f"pid {pid} result={result}")
            continue
        try:
            rev = int(revision)
        except ValueError:
            bad.append(f"pid {pid} revision={revision}")
            continue
        if rev < min_revision:
            bad.append(f"pid {pid} revision {rev} < R0 {min_revision}")
        if rev == min_revision and r0_prefix and prefix != r0_prefix[:12]:
            bad.append(f"pid {pid} same revision as R0 but different content_sha256 (same revision, different digest)")
    if len(by_pid) != workers:
        bad.append(f"{len(by_pid)} distinct pids != {workers} workers")
    if bad:
        return False, out + ["WARMUP_FAIL " + "; ".join(bad)]
    return True, out + [f"WARMUP_OK workers={workers} all success, revision >= {min_revision}"]


def cmd_warmup_check(args: argparse.Namespace) -> int:
    ok, lines = warmup_check(args.journal.read_text(encoding="utf-8", errors="replace"), args.workers, args.min_revision, args.r0_content_sha256)
    print("\n".join(lines))
    return 0 if ok else 1


# ---------------------------------------------------------------- selftest
# Path-token corpus (review wac-032-r3 §2.2, scratchpad r3probe/newshapes.py). Shared with
# tests/site_check_leak_test.sh, which runs the same corpus through the site check's own copy.
PATH_SHAPES_R3 = (
    ("/hook/SENTINEL%2Fq1w2%3De3r4t5/*", ("SENTINEL%2Fq1w2", "q1w2%3De3r4t5")),                 # N1 url-encoded
    ("/hook/qwertyASDFGH%2Bzxcvbn", ("qwertyASDFGH%2Bzxcvbn", "qwertyASDFGH")),                  # N1b
    ("/api/x?token=pw7qs&sig=SENTINELsig0123456789", ("SENTINELsig0123456789",)),                # N2 query k=v
    ("/api?SENTINELtoken0123abc", ("SENTINELtoken0123abc",)),                                   # N2b query, no '='
    (r"^/bot1234567890\:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/.*$", ("AAHdqTcvCH1v", "1234567890")),  # N3 escaped ':'
    (r"^/hook/ab12cd34\-ef56gh78\-ij90kl12$", ("ab12cd34", "ef56gh78", "ij90kl12")),            # N3b escaped '-'
    (r"^/k/SENTINELre\.gex0123456789$", ("SENTINELre", "gex0123456789")),                        # N3c escaped '.'
    ("/n/4829105738291045", ("4829105738291045",)),                                             # N4 16 digits (hex rule)
    ("/h/DEADBEEFCAFEBABE", ("DEADBEEFCAFEBABE",)),                                             # N4b letter-only hex
    ("/jwt/eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV",
     ("eyJhbGciOiJIUzI1NiJ9", "SflKxwRJSMeKKF2QT4fw")),                                        # N5 JWT
    ("/k/ab12cd34ef.56gh78ij90", ("ab12cd34ef", "56gh78ij90")),                                 # N5b dotted short parts
    ("/hook/3f2504e0-4f89-11d3-9a0c-0305e82c3301", ("3f2504e0", "0305e82c3301")),               # N6 UUID
    ("/k/Ab3dEfG5hI+x/jK7lMnO9pQ==", ("Ab3dEfG5hI", "jK7lMnO9pQ")),                             # N7 raw base64
    ("/bot1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/*", ("AAHdqTcvCH1vGWJx",)),             # N9 unescaped bot token
    ("(?i)^/hook/(?:SENTINELgrp0123abcd)$", ("SENTINELgrp0123abcd",)),                          # N10 (?i) group
    ("/t/a1b2c3d4e5f6", ("a1b2c3d4e5f6",)),                                                     # exactly 12 mixed
    ("/n/4829-1057-3829-1045", ("4829-1057", "3829-1045")),                                     # dash-separated digits (hex rule after separators)
    ("/k/a1.b2.c3.d4.e5", ("a1.b2.c3", "c3.d4.e5")),                                            # short dotted parts, 12+ only with the dots
    ("/n/4829+1057+3829+1045", ("4829+1057", "3829+1045", "48291057")),                         # M3 (wac-073): '+'-separated digits
    ("/n/4829=1057=3829=1045", ("4829=1057", "3829=1045", "48291057")),                         # M4 (wac-073): '='-separated digits
    ("^/n/4829\\+1057\\+3829\\+1045$", ("4829", "3829")),                                         # M3 as an escaped regex
)
# exact outputs that pin each rule on its own (a removed rule or a moved threshold changes them)
PATH_RULE_PINS = (
    ("/n/4829105738291045", "/n/<seg len=16>"),       # hex rule only (no letter)
    ("/h/DEADBEEFCAFEBABE", "/h/<seg len=16>"),       # hex rule only (no digit)
    ("/n/482910573829104", "/n/482910573829104"),     # 15 digits: below the hex threshold (documented residual)
    ("/t/a1b2c3d4e5f6", "/t/<seg len=12>"),           # exactly 12 with letter and digit
    ("/p/a1b2c3d4e5f", "/p/a1b2c3d4e5f"),             # 11: below the threshold (documented residual)
    (r"^/hook/ab12cd34\-ef56gh78\-ij90kl12$", "^/hook/<seg len=29>"),   # whole chunk, not pieces
    ("/k/Ab3dEfG5hI+x/jK7lMnO9pQ==", "/k/<seg len=12>/<seg len=12>"),  # '+' and '=' are token characters
    ("/n/4829-1057-3829-1045", "/n/<seg len=19>"),     # separators removed before the hex rule
    ("/k/a1.b2.c3.d4.e5", "/k/<seg len=14>"),          # judged with the separators too
    ("/n/4829+1057+3829+1045", "/n/<seg len=19>"),     # '+' dropped in the second pass (wac-073 M3)
    ("/n/4829=1057=3829=1045", "/n/<seg len=19>"),     # '=' dropped in the second pass (wac-073 M4)
    ("/n/4829+1057+3829+104", "/n/4829+1057+3829+104"),  # 15 digits after dropping '+': below the threshold, whole chunk kept
)
LEGIT_PATHS = (
    "/m/v1/watcher/dialogs", "/m/v1/watcher/disconnect", "/m/v1/watcher/groups", "/m/v1/watcher/media/*", "/m/v1/watcher/reconnect",
    "/m/v1/watcher/status", "/m/v1/watcher/trading/accounts", "/m/v1/watcher/trading/accounts/*", "/m/v1/watcher/trading/briefings",
    "/m/v1/watcher/trading/channels", "/m/v1/watcher/trading/channels/*", "/m/v1/watcher/trading/messages", "/m/v1/watcher/trading/orders",
    "/m/v1/watcher/trading/orders/active", "/m/v1/watcher/trading/risks", "/m/v1/watcher/trading/risks/*",
    "/watcher/*", "/api/price-alerts/*", "/api/price-monitor/*", "/v1/nodes/[^/]+/status", "^/m/v1/watcher/trading/risks/[^/]+$",
    "/.well-known/acme-challenge/*", "/m/v1/watcher/config/revision", "{http.request.uri}", "/api/login/*",
    "/m/v1/watcher/trading/price-alerts/[^/]+$", "(?i:/m/v1/watcher)(?:/|$)", "^(?i:/m/v1/watcher)(?:/|$)",
    "/api{http.request.uri.path.1}",   # a placeholder is structure: it is removed before a chunk is judged
    "^(?i:/m/v1/watcher)(?:[/\\n]|$)", "/m/v1/watcher/trading/accounts/{account_id}",   # WGW-1.0.2 fallback and a v2 template (wac-060)
    "/etc/caddy/caddy-watcher-gateway.caddy",
)


def _selftest_wgw(base: Path, cp: Path, good_shows: dict, show, U_OQ: str, U_NC: str, U_EI: str, oq_env_f: Path) -> int:
    """WGW-1.0.4 stage O helpers (task wac-105): RS-17 four-unit isolation, the env whitelist builder, the shared directory
    manifest / drift report / snapshot, NeedDaemonReload, port checks, V-5 (RS-19) and its evidence check. Fake values only."""
    import contextlib
    import io
    import secrets
    import subprocess
    import threading
    checks = 0
    tok = lambda: "o0fake" + secrets.token_urlsafe(32)  # noqa: E731
    readers = {n: tok() for n in WGW_READER_TOKENS}
    live_oq = base / "wgw-live-operator-query.env"
    live_oq.write_text("".join(f"{k}={v}\n" for k, v in readers.items()) + "SIGNAL_TOKEN_ACCOUNT_A=" + tok() + "\nAUTH_SECRET_KEY=" + tok()
                       + "\nDATABASE_URL=postgres://SENTINELdb\n", encoding="utf-8")
    frag = base / "wgw-fragment.env"
    frag.write_text(f"WATCHER_GATEWAY_TOKEN={tok()}\n", encoding="utf-8")

    def run(argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(argv)
        return rc, buf.getvalue()
    # wgw-env: names only, 0600, never overwritten, signal / DB / AUTH_SECRET_KEY left out by default
    out = base / "wgw.env"
    rc, text = run(["wgw-env", "--oq-env", str(live_oq), "--gateway-fragment", str(frag), "--out", str(out)])
    env = parse_env_file(out)
    assert rc == 0 and set(env) == {WGW_GATEWAY_TOKEN, *WGW_READER_TOKENS} and oct(out.stat().st_mode & 0o777) == "0o600", (rc, text)
    assert "o0fake" not in text and "SENTINEL" not in text, "wgw-env printed a value"
    rc2, text2 = run(["wgw-env", "--oq-env", str(live_oq), "--gateway-fragment", str(frag), "--out", str(out)])
    assert rc2 == 1 and "WGW_ENV_REFUSED" in text2, "an existing env file is never overwritten"
    rc3, text3 = run(["wgw-env", "--check", str(out)])
    assert rc3 == 0 and "WGW_ENV_OK" in text3, text3
    out_a = base / "wgw-auth.env"
    rc4, _t = run(["wgw-env", "--oq-env", str(live_oq), "--gateway-fragment", str(frag), "--out", str(out_a), "--with-auth-secret-key"])
    assert rc4 == 0 and WGW_AUTH_SECRET in parse_env_file(out_a)
    assert run(["wgw-env", "--check", str(out_a)])[0] == 1, "AUTH_SECRET_KEY without U-13 (iii) must fail the check"
    # collision: a reader equal to the gateway token; a short reader; a missing reader
    for name, text_env in (("gateway == reader", "".join(f"{k}={v}\n" for k, v in readers.items()) + f"WATCHER_GATEWAY_TOKEN={readers['VIEWER_TOKEN']}\n"),
                           ("short reader", f"WATCHER_GATEWAY_TOKEN={tok()}\nRISK_ADMIN_TOKEN=short\nVIEWER_TOKEN={tok()}\nREVIEWER_TOKEN={tok()}\nSYSTEM_OBSERVER_TOKEN={tok()}\n"),
                           ("missing reader", f"WATCHER_GATEWAY_TOKEN={tok()}\n"),
                           ("signal token present", "".join(f"{k}={v}\n" for k, v in readers.items()) + f"WATCHER_GATEWAY_TOKEN={tok()}\nSIGNAL_TOKEN_ACCOUNT_B={tok()}\n")):
        f = base / f"wgw-bad-{abs(hash(name))}.env"
        f.write_text(text_env, encoding="utf-8")
        os.chmod(f, 0o600)
        rc_b, text_b = run(["wgw-env", "--check", str(f)])
        assert rc_b == 1 and "o0fake" not in text_b, (name, text_b)
        checks += 1
    checks += 4
    # RS-17: four units
    rel = base / "releases" / "watcher-gateway"
    wd = rel / ("0" * 40) / "services" / "control-plane" / "api"
    wgw_show = ("LoadState=loaded\nNeedDaemonReload=no\nFragmentPath=/etc/systemd/system/w.service\nUser=trader-v3-cp-watcher-gateway\n"
                f"WorkingDirectory={wd}\nEnvironmentFiles={out} (ignore_errors=no)\n"
                "Environment=CONTROL_PLANE_APP_ROLE=watcher-gateway PYTHONDONTWRITEBYTECODE=1\n"
                "ExecStart={ path=/srv/trader-v3/.venv-cp/bin/uvicorn ; argv[]=/srv/trader-v3/.venv-cp/bin/uvicorn read_api:app --host 127.0.0.1 --port 8186 ; }\n")
    U_W = WGW_UNIT

    def iso4(shows, **kw):
        return cp_isolation_check(shows, U_OQ, [U_NC, U_EI], str(oq_env_f), str(cp), U_W, str(out), str(rel), **kw)
    shows4 = dict(good_shows, **{U_W: wgw_show})
    rc_i, lines_i = iso4(shows4)
    assert rc_i == 0 and "CP_ISOLATION_OK units=4 watcher_gateway=present" in lines_i[-1], lines_i
    rc_i, lines_i = iso4(dict(good_shows, **{U_W: "LoadState=not-found\nNeedDaemonReload=no\n"}), wgw_may_be_absent=True)
    assert rc_i == 0 and "watcher_gateway=absent" in lines_i[-1], lines_i
    assert iso4(dict(good_shows, **{U_W: "LoadState=not-found\nNeedDaemonReload=no\n"}))[0] == 2, "absent unit only when allowed"
    for name, patch_ in (("port 8183", ("--port 8186", "--port 8183")), ("host 0.0.0.0", ("--host 127.0.0.1", "--host 0.0.0.0")),
                         ("role all", ("ROLE=watcher-gateway", "ROLE=all")), ("DB role expected", ("PYTHONDONTWRITEBYTECODE=1", "CONTROL_PLANE_EXPECT_DATABASE_ROLE=x")),
                         ("shared code dir", (str(wd), str(cp / "api"))), ("other user", ("User=trader-v3-cp-watcher-gateway", "User=root")),
                         ("second env file", (f"EnvironmentFiles={out} (ignore_errors=no)", f"EnvironmentFiles={out} (ignore_errors=no)\nEnvironmentFiles={oq_env_f} (ignore_errors=no)"))):
        rc_i, lines_i = iso4(dict(good_shows, **{U_W: wgw_show.replace(*patch_)}))
        assert rc_i == 1, (name, lines_i)
        checks += 1
    rc_i, lines_i = iso4(dict(shows4, **{U_NC: show(cp / "api", [out], role="node-control")}))
    assert rc_i == 1 and any("loads " + str(out) in l for l in lines_i), ("node-control loads the watcher-gateway env", lines_i)
    rc_i, lines_i = iso4(dict(shows4, **{U_W: wgw_show.replace(str(out), str(out_a))}))
    assert rc_i == 1, "an env file other than --wgw-env"
    shutil_out = base / "wgw-auth-copy.env"
    shutil_out.write_bytes(out_a.read_bytes())
    rc_i, lines_i = cp_isolation_check(dict(good_shows, **{U_W: wgw_show.replace(str(out), str(shutil_out))}), U_OQ, [U_NC, U_EI], str(oq_env_f), str(cp),
                                       U_W, str(shutil_out), str(rel), allow_auth_secret=True)
    assert rc_i == 0, ("AUTH_SECRET_KEY allowed only with --allow-auth-secret-key", lines_i)
    assert "o0fake" not in "\n".join(lines_i)
    checks += 4
    # shared directory: 67b401a manifest, drift report with suggestions (never a repair), acceptance bound to the list, snapshot
    root = base / "trader-root"
    (root / "services/control-plane/api").mkdir(parents=True)
    (root / "packages/execution-domain").mkdir(parents=True)
    (root / "services/control-plane/api/read_api.py").write_text("print(1)\n")
    (root / "packages/execution-domain/a.py").write_text("x = 1\n")
    man = base / "cp-shared.baseline.sha256"
    man.write_text(f"{sha256_file(root / 'services/control-plane/api/read_api.py')}  services/control-plane/api/read_api.py\n"
                   f"{sha256_file(root / 'packages/execution-domain/a.py')}  packages/execution-domain/a.py\n", encoding="utf-8")
    snap = base / "shared-before.json"
    rc, text = run(["shared-tree", "--root", str(root), "--manifest", str(man), "--snapshot-out", str(snap)])
    assert rc == 0 and "MANIFEST_OK cp-shared-vs-67b401a compared=2" in text and snap.is_file(), text
    assert run(["shared-tree", "--root", str(root), "--compare", str(snap)])[1].startswith("SHARED_UNCHANGED")
    hot = root / "services/control-plane/api/read_api.py"
    hot.write_text("print('hotfix')\n")
    (root / "services/control-plane/api/extra_hotmount.py").write_text("y = 2\n")
    before_bytes = hot.read_bytes()
    rc, text = run(["shared-tree", "--root", str(root), "--manifest", str(man)])
    drift_sha = re.search(r"drift_sha256=([0-9a-f]{64})", text).group(1)
    assert rc == 1 and "DRIFT MODIFIED services/control-plane/api/read_api.py" in text and "SUGGESTION option A" in text \
        and "INFO EXTRA services/control-plane/api/extra_hotmount.py" in text and hot.read_bytes() == before_bytes, text
    rc, text = run(["shared-tree", "--root", str(root), "--manifest", str(man), "--accept-drift-sha256", drift_sha])
    assert rc == 0 and "MANIFEST_DRIFT_ACCEPTED" in text, text
    assert run(["shared-tree", "--root", str(root), "--manifest", str(man), "--accept-drift-sha256", "0" * 64])[0] == 1, "acceptance is bound to the list"
    rc, text = run(["shared-tree", "--root", str(root), "--compare", str(snap)])
    assert rc == 1 and "SHARED_CHANGED changed=1 added=1" in text, text
    checks += 5
    # NeedDaemonReload over every unit
    show_all = "Id=caddy.service\nNeedDaemonReload=no\n\nId=trader-v3-controlplane-operator-query.service\nNeedDaemonReload=no\n\nId=x.timer\nNeedDaemonReload=no\n"
    assert daemon_reload_check(show_all, [], ["caddy.service"])[0] == 0
    assert daemon_reload_check(show_all.replace("x.timer\nNeedDaemonReload=no", "x.timer\nNeedDaemonReload=yes"), [], [])[0] == 1
    assert daemon_reload_check(show_all + f"\nId={WGW_UNIT}\nNeedDaemonReload=yes\n", [WGW_UNIT], [])[0] == 0, "the unit this step installed"
    assert daemon_reload_check("", [], [])[0] == 2 and daemon_reload_check(show_all, [], ["missing.service"])[0] == 2
    assert daemon_reload_check("Id=a.service\n", [], [])[0] == 1, "a missing property is not 'no'"
    checks += 5
    # ports (PC-6 (ii), S-06)
    ss = ('LISTEN 0 4096 127.0.0.1:8183 0.0.0.0:* users:(("uvicorn",pid=1,fd=3))\n'
          'LISTEN 0 4096 127.0.0.1:8185 0.0.0.0:* users:(("other",pid=2,fd=3))\n')
    assert port_check(ss, "8186", "free", (8184, 8189))[0] == 0 and any("RECORD port 8185" in l for l in port_check(ss, "8186", "free", (8184, 8189))[1])
    assert port_check(ss + 'LISTEN 0 4096 127.0.0.1:8186 0.0.0.0:* users:(("uvicorn",pid=3,fd=3))\n', "8186", "free", None)[0] == 1
    assert port_check(ss + 'LISTEN 0 4096 127.0.0.1:8186 0.0.0.0:*\n', "8186", "loopback-only", None)[0] == 0
    assert port_check(ss + 'LISTEN 0 4096 0.0.0.0:8186 0.0.0.0:*\n', "8186", "loopback-only", None)[0] == 1
    assert port_check(ss, "8186", "loopback-only", None)[0] == 1
    checks += 5
    # V-5 against a local http stand-in (the real run is https on 443 from outside jp-24): the closed status sets
    import http.server

    def v5_server(codes: dict):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.headers.get("Authorization") or self.headers.get("Cookie"):
                    code = 599
                else:
                    code = codes.get(self.path, 200)
                self.send_response(code); self.send_header("Content-Length", "0"); self.end_headers()

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return srv
    good_codes = {"/v1/watcher/status": 404, "/v1/watcher/dialogs": 404, "/V1/WATCHER/status": 404, "/m/v1/watcher/status": 401, "/m/v1/accounts": 401}
    for phase, codes, want in (("unit-running", good_codes, 0), ("before-unit", dict(good_codes, **{"/m/v1/watcher/status": 502}), 0),
                               ("unit-running", dict(good_codes, **{"/m/v1/watcher/status": 502}), 1),
                               ("unit-running", dict(good_codes, **{"/v1/watcher/status": 200}), 1),
                               ("unit-running", dict(good_codes, **{"/m/v1/accounts": 200}), 1),
                               ("before-unit", good_codes, 1)):
        srv = v5_server(codes)
        try:
            rc, text = run(["public-direct-check", "--host", "localhost", "--phase", phase, "--scheme", "http", "--port", str(srv.server_address[1])])
        finally:
            srv.shutdown(); srv.server_close()
        assert rc == want and ("V5_PUBLIC_CHECK_OK mode=test" in text) == (want == 0), (phase, codes, text)
        checks += 1
    ev = ("\n".join(f"V5 GET {p_} status=404 content_type=- size=0 want=404 ok" for p_, _k in V5_REQUESTS)
          + f"\nV5_PUBLIC_CHECK_OK mode=external phase=unit-running host=jp-bot.balen.wang at=x epoch=1000 checks=5\n")
    assert v5_evidence_check(ev, "jp-bot.balen.wang", "unit-running", 900, 3600, now=1100)[0]
    assert not v5_evidence_check(ev.replace("mode=external", "mode=loopback"), "jp-bot.balen.wang", "unit-running", 900, 3600, now=1100)[0]
    assert not v5_evidence_check(ev, "jp-bot.balen.wang", "unit-running", 1200, 3600, now=1300)[0], "older than the unit start"
    assert not v5_evidence_check(ev, "jp-bot.balen.wang", "unit-running", 900, 60, now=2000)[0], "too old"
    assert not v5_evidence_check(ev.replace(" ok\nV5_PUBLIC", " FAIL\nV5_PUBLIC"), "jp-bot.balen.wang", "unit-running", 900, 3600, now=1100)[0]
    rc, text = run(["v5-evidence-check", "--host", "jp-bot.balen.wang"])
    assert rc == 1 and text.startswith("DIRECT_GUARD_UNVERIFIED"), text
    checks += 6
    # the unit file: render == lint-clean; each deviation is caught
    conf = "[Service]\nMemoryMax=512M\nRestart=on-failure\n"
    sha = "a" * 40
    unit = render_wgw_unit("/srv/trader-v3", sha, conf)
    assert wgw_unit_lint(unit, "/srv/trader-v3", sha) == [] and "MemoryMax=512M" in unit and "[Service]\n[Service]" not in unit, unit
    for name, bad_unit in (("port 8183", unit.replace("--port 8186", "--port 8183")), ("pg_isready", unit.replace("TimeoutStopSec", "ExecStartPre=+/usr/bin/docker exec x pg_isready\nTimeoutStopSec")),
                           ("DB role", unit.replace("PYTHONDONTWRITEBYTECODE=1", "PYTHONDONTWRITEBYTECODE=1\nEnvironment=CONTROL_PLANE_EXPECT_DATABASE_ROLE=x")),
                           ("shared dir", unit.replace(f"releases/watcher-gateway/{sha}/", "")), ("other env file", unit.replace("watcher-gateway.env", "operator-query.env")),
                           ("0.0.0.0", unit.replace("--host 127.0.0.1", "--host 0.0.0.0")), ("root user", unit.replace(f"User={WGW_USER}", "User=root"))):
        assert wgw_unit_lint(bad_unit, "/srv/trader-v3", sha), ("unit lint missed", name)
        checks += 1
    # the RS-16 dependency check (python -B import smoke) on a fake code tree
    tr = base / "smoke-root"
    api = tr / "releases" / "watcher-gateway" / sha / "services" / "control-plane" / "api"
    api.mkdir(parents=True)
    (tr / "services" / "control-plane").mkdir(parents=True)
    (tr / "services" / "control-plane" / "shared.txt").write_text("shared\n")
    fake_venv = base / "fake-venv"
    (fake_venv / "bin").mkdir(parents=True)
    (fake_venv / "bin" / "python").symlink_to(sys.executable)
    good_mod = ("class R:\n    def __init__(s, n, p): s.name, s.path = n, p\n"
                "class A:\n    routes = [R('watcher_gateway__status', '/v1/watcher/status'), R('role_database_health', '/health/role')]\n"
                "def create_app(role):\n    assert role == 'watcher-gateway'\n    return A()\n")
    (api / "watcher_gateway.py").write_text("import os\ndef readiness():\n    return (bool(os.environ.get('WATCHER_GATEWAY_TOKEN')), 'ok')\n")

    def smoke(read_api_text):
        (api / "read_api.py").write_text(read_api_text)
        return run(["wgw-smoke", "--code-dir", str(tr / "releases" / "watcher-gateway" / sha), "--venv", str(fake_venv), "--env-file", str(out),
                    "--trader-root", str(tr)])
    rc, text = smoke(good_mod)
    assert rc == 0 and "SMOKE_OK" in text and "gateway_routes=1" in text and "o0fake" not in text, text
    assert not list(api.rglob("__pycache__")), "python -B must not write __pycache__"
    rc, text = smoke("import o0_no_such_module_httpx_stand_in\n" + good_mod)
    assert rc == 3 and "DEPENDENCY_MISSING o0_no_such_module_httpx_stand_in" in text, text
    rc, text = smoke(f"open({str(tr / 'services' / 'control-plane' / 'shared.txt')!r}).read()\n" + good_mod)
    assert rc == 1 and "under TRADER_ROOT outside the code dir" in text, text
    rc, text = smoke("open(__file__ + '.written', 'w').write('x')\n" + good_mod)
    assert rc == 1 and "wrote into the code dir" in text, text
    (api / "read_api.py.written").unlink()
    rc, text = smoke(good_mod.replace("R('watcher_gateway__status', '/v1/watcher/status'), ", ""))
    assert rc == 1 and "0 gateway routes" in text, text
    checks += 5
    return checks


def cmd_selftest(_args: argparse.Namespace) -> int:
    import contextlib
    import io
    base = Path(tempfile.mkdtemp(prefix="o0-tool-selftest-"))
    checks = 0
    try:
        # fleet
        def w(name: str, text: str) -> Path:
            path = base / name
            path.write_text(text, encoding="utf-8")
            return path
        nodes = "account-a account-b".split()
        ports = "8081 8082".split()
        good = "account-a ACTIVE abcdef012345 hb_age=1.0\naccount-b HALTED abcdef012345 hb_age=0.4\nready:8081 200\nready:8082 503\n"
        cases = {
            "empty vs empty": ("", "", 2),
            "partial node set": ("account-a ACTIVE abcdef012345 hb_age=1.0\nready:8081 200\nready:8082 503\n", good, 2),
            "frozen heartbeat after": (good, good.replace("hb_age=1.0", "hb_age=412.7"), 3),
            "age jump below the freeze threshold": (good.replace("hb_age=0.4", "hb_age=0.1"), good.replace("hb_age=0.4", "hb_age=4.9"), 0),
            "status change": (good, good.replace("account-a ACTIVE", "account-a HALTED"), 3),
            "release change": (good, good.replace("account-b HALTED abcdef012345", "account-b HALTED 999999999999"), 3),
            "ready change": (good, good.replace("ready:8081 200", "ready:8081 000"), 3),
            "stale before": (good.replace("hb_age=1.0", "hb_age=30.0"), good, 2),
            "unparsable row": (good, good + "account-c\n", 2),
            "missing ready line": (good, good.replace("ready:8082 503\n", ""), 2),
            "unchanged": (good, good.replace("hb_age=1.0", "hb_age=2.2"), 0),
        }
        for name, (b, a, want) in cases.items():
            rc, lines = fleet_compare(w("b.txt", b), w("a.txt", a), nodes, ports, 5.0, 5.0, [])
            assert rc == want, (name, rc, lines)
            checks += 1
        rc, lines = fleet_compare(w("b.txt", good), None, nodes, ports, 5.0, 5.0, [])
        assert rc == 0 and lines[-1].startswith("FLEET_BASELINE_OK"), lines
        rc, _ = fleet_compare(base / "missing.txt", w("a.txt", good), nodes, ports, 5.0, 5.0, [])
        assert rc == 2
        down = good.replace("account-b HALTED abcdef012345 hb_age=0.4", "account-b HALTED abcdef012345 hb_age=9999.0")
        rc, _ = fleet_compare(w("b.txt", down), w("a.txt", down), nodes, ports, 5.0, 5.0, ["account-b"])
        assert rc == 0, "a declared known-down node is compared on status/release only"
        checks += 3
        # gates
        bundle = base / "bundle"
        bundle.mkdir()
        (bundle / "RELEASE.json").write_text(json.dumps({"candidate": "c" * 40, "deploy_candidate": True}), encoding="utf-8")
        (bundle / "SHA256SUMS").write_text("x  a\n", encoding="utf-8")
        cand = base / "Caddyfile.candidate"
        cand.write_text("site {}\n", encoding="utf-8")
        gate = base / "caddy.gate.json"
        with contextlib.redirect_stdout(io.StringIO()):
            assert cmd_gate_write(argparse.Namespace(bundle=bundle, stage="caddy-preflight", out=gate, field=["x=1"], file_sha=[f"candidate_caddyfile={cand}"])) == 0
        exp = {"candidate_caddyfile": sha256_file(cand)}
        assert gate_check(gate, "caddy-preflight", bundle, 3600, exp)[0]
        assert not gate_check(base / "nope.json", "caddy-preflight", bundle, 3600, exp)[0], "missing gate accepted"
        assert not gate_check(gate, "watcher-build", bundle, 3600, exp)[0], "wrong stage accepted"
        assert not gate_check(gate, "caddy-preflight", bundle, 3600, {"candidate_caddyfile": "0" * 64})[0], "different candidate file accepted"
        assert not gate_check(gate, "caddy-preflight", bundle, 3600, {"image_id": "x"})[0], "missing field accepted"
        assert not gate_check(gate, "caddy-preflight", bundle, 3600, exp, now=time.time() + 7200)[0], "stale gate accepted"
        (bundle / "SHA256SUMS").write_text("y  a\n", encoding="utf-8")
        assert not gate_check(gate, "caddy-preflight", bundle, 3600, exp)[0], "modified bundle accepted"
        (bundle / "SHA256SUMS").write_text("x  a\n", encoding="utf-8")
        (bundle / "RELEASE.json").write_text(json.dumps({"candidate": "d" * 40, "deploy_candidate": True}), encoding="utf-8")
        assert not gate_check(gate, "caddy-preflight", bundle, 3600, exp)[0], "different candidate accepted"
        # review wac-032-r2 🟡-3: put the bundle back first, so the ONLY difference is ok:false
        (bundle / "RELEASE.json").write_text(json.dumps({"candidate": "c" * 40, "deploy_candidate": True}), encoding="utf-8")
        assert gate_check(gate, "caddy-preflight", bundle, 3600, exp)[0], "the restored bundle must pass again (else the next check is vacuous)"
        good_record = gate.read_text()
        failed = json.loads(good_record)
        failed["ok"] = False
        gate.write_text(json.dumps(failed))
        ok_false, why = gate_check(gate, "caddy-preflight", bundle, 3600, exp)
        assert not ok_false and "not a passed" in why, ("failed gate accepted", why)
        for bad_ok in ("true", 1, None):
            failed["ok"] = bad_ok
            gate.write_text(json.dumps(failed))
            assert not gate_check(gate, "caddy-preflight", bundle, 3600, exp)[0], f"gate with ok={bad_ok!r} accepted"
        gate.write_text(good_record)
        (bundle / "RELEASE.json").write_text(json.dumps({"candidate": "c" * 40, "deploy_candidate": False}), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            assert cmd_gate_write(argparse.Namespace(bundle=bundle, stage="s", out=base / "g2.json", field=[], file_sha=[])) == 1, "gate written for a non-candidate"
        checks += 14
        # JSON redaction: the three shapes from review wac-032 🔴-5 plus the old ones
        s1, s2, s3, s4, s5 = ("SENTINELmatcherBEARERxyz0123456789abcdefgh", "SENTINELreplaceOP", "SENTINELstaticBODY",
                              "$2a$14$SENTINELbcryptHASHabcdefghijklmnopqrstuvwxyzABCDEFGH01", "shortpw")
        cfg = {"apps": {"http": {"servers": {"srv0": {"listen": [":443"], "routes": [
            {"match": [{"header": {"Authorization": [f"Bearer {s1}"]}, "path": ["/hook/*"]}],
             "handle": [{"handler": "headers", "request": {"replace": {"X-Up": [{"search": "a", "replace": s2}]}, "delete": ["X-Watcher-Actor"]}},
                        {"handler": "static_response", "status_code": 200, "body": s3}]},
            {"handle": [{"handler": "authentication", "providers": {"http_basic": {"accounts": [{"username": "u", "password": s4}]}}},
                        {"handler": "reverse_proxy", "upstreams": [{"dial": "127.0.0.1:9090"}],
                         "headers": {"request": {"set": {"X-Watcher-Proxy-Auth": ["{env.WATCHER_BROWSER_PROXY_TOKEN}"], "X-Other": [s5]}}}}]},
            {"handle": [{"handler": "vars", "api": s5}, {"handler": "rewrite", "uri": "/x?token=" + s5}]},
        ]}}}}}
        for fn in (_redact_json, caddy_skeleton):
            text = json.dumps(fn(cfg))
            for sentinel in (s1, s2, s3, s4, "SENTINEL", s5):
                assert sentinel not in text, (fn.__name__, sentinel)
            assert "{env.WATCHER_BROWSER_PROXY_TOKEN}" in text and "X-Watcher-Actor" in text and "127.0.0.1:9090" in text, fn.__name__
            checks += 1
        # review wac-032-r2 🔴-1: secrets inside paths (probe token, 40-hex webhook token,
        # bot token in a path_regexp, token in a rewrite uri) + legitimate paths kept as is
        p1, p2, p3, p4 = ("qctbhy0a32hucub", "9f8e7d6c5b4a39281706f5e4d3c2b1a0ffeeddcc",
                          "bot7654321098:AAHpathBOTsentinel0123456789abcd", "SENTINELpathsecretXYZ0123456789abcdef")
        good_re = "^/m/v1/watcher/trading/risks/[^/]+$"
        pcfg = {"apps": {"http": {"servers": {"srv0": {"listen": [":443"], "routes": [
            {"match": [{"path": ["/" + p1]}], "handle": [{"handler": "static_response", "status_code": 200}]},
            {"match": [{"path": [f"/hook/{p2}/*", "/m/v1/watcher/media/*"]}], "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": "127.0.0.1:7000"}]}]},
            {"match": [{"path_regexp": {"name": "tg", "pattern": f"^/{p3}/.*$"}}], "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": "127.0.0.1:7001"}]}]},
            {"match": [{"path_regexp": {"name": "wgw_risk", "pattern": good_re}}], "handle": [{"handler": "rewrite", "uri": "/api/" + p4, "strip_path_prefix": "/m"}]},
        ]}}}}}
        for fn in (_redact_json, caddy_skeleton):
            out = fn(pcfg)
            text = json.dumps(out)
            for needle in (p1, p2, p3, p4, "qctbhy", "9f8e7d6c5b4a", "AAHpathBOT", "SENTINELpath", "7654321098"):
                assert needle not in text, (fn.__name__, "path secret leaked", needle)
            routes = out["apps"]["http"]["servers"]["srv0"]["routes"]
            assert routes[0]["match"][0]["path"] == ["/<seg len=15>"], (fn.__name__, routes[0])
            assert routes[1]["match"][0]["path"] == ["/hook/<seg len=40>/*", "/m/v1/watcher/media/*"], (fn.__name__, routes[1])
            assert routes[2]["match"][0]["path_regexp"]["pattern"] == "^/<seg len=46>/.*$", (fn.__name__, routes[2])
            assert routes[3]["match"][0]["path_regexp"]["pattern"] == good_re, (fn.__name__, "legitimate pattern over-redacted")
            assert routes[3]["handle"][0]["uri"] == "/api/<seg len=37>" and routes[3]["handle"][0]["strip_path_prefix"] == "/m", (fn.__name__, routes[3])
            checks += 1
        for line, needle in ((f"	handle /{p1} {{", p1), (f"+	handle /hook/{p2}/* {{", p2), (f"	@tg path_regexp ^/{p3}/.*$", "AAHpath"),
                             (f"	rewrite * /api/{p4}", p4)):
            assert needle not in redact_line(line), ("redact (Caddyfile diff lines)", line)
        for keep in (f"	@wgw_risk path_regexp {good_re}", "	reverse_proxy 127.0.0.1:8183", "	handle_path /m/v1/watcher/media/* {"):
            assert redact_line(keep) == keep, ("over-redaction", keep)
        checks += 1
        # review wac-032-r3 🟡-1 / 🟡-3: every r3probe shape (escaped '-', escaped '.', dotted
        # short parts, raw base64 incl. '+' and '=='), the hex rule on its own (digit-only and
        # letter-only hex carry no letter+digit mix), the exact 12-char threshold; 0 leaks in
        # every output, and the legitimate paths (16 generated gateway paths + 12) kept as is.
        for shape, needles in PATH_SHAPES_R3:
            cfg3 = {"apps": {"http": {"servers": {"s": {"routes": [
                {"match": [{"path": [shape]}, {"path_regexp": {"name": "n", "pattern": shape}}],
                 "handle": [{"handler": "rewrite", "uri": shape, "strip_path_prefix": shape}, {"handler": "file_server", "root": shape}]}]}}}}}
            outputs = [json.dumps(_redact_json(cfg3)), json.dumps(caddy_skeleton(cfg3)), redact_path(shape)]
            outputs += [redact_line(f"	{d} {shape}") for d in ("handle", "rewrite *", "@m path_regexp", "root *", "handle_path", "uri strip_prefix")]
            for out_text in outputs:
                for needle in needles:
                    assert needle not in out_text, ("r3 path shape leaked", shape, needle, out_text)
            checks += 1
        for shape, want in PATH_RULE_PINS:
            assert redact_path(shape) == want, ("path rule pin", shape, redact_path(shape), want)
            checks += 1
        over = [p for p in LEGIT_PATHS if redact_path(p) != p or redact_line(p) != p]
        assert not over, ("legitimate paths over-redacted", over)
        checks += 1
        # fleet guard parameters vs production node heartbeat parameters (review wac-032-r2 🟡-7)
        defaults = dict(interval=2.0, timeout=15.0, max_age=5.0, max_jump=5.0, settle=60.0, samples=4, sample_interval=20.0)
        def fp(**kw):
            a = dict(defaults, **kw)
            return fleet_params_check(a["interval"], a["timeout"], a["max_age"], a["max_jump"], a["settle"], a["samples"], a["sample_interval"])
        assert fp()[0] == 0, fp()
        for kw, what in ((dict(interval=4.0), "slower heartbeat, default age 5 < 8: late beats read as frozen"),
                         (dict(interval=1.0), "faster heartbeat, age 5 > 3: dead node seen late"),
                         (dict(timeout=60.0), "settle 60 < 60 + 6"),
                         (dict(max_jump=9.0), "jump outside the band"),
                         (dict(max_age=9.0), "age outside the band on its own (jump still inside)"),
                         (dict(settle=20.0), "settle 20 < 21"),
                         (dict(interval=0.0), "non-positive interval")):
            rc_p, lines_p = fp(**kw)
            assert rc_p == 2, (what, lines_p)
            checks += 1
        assert fp(interval=4.0, max_age=10.0, max_jump=10.0)[0] == 0, "a consistent non-default set passes"
        assert fp(settle=21.0)[0] == 0 and fp(settle=20.9)[0] == 2, "settle bound is timeout + 3 x interval exactly"
        assert "O0_FLEET_MAX_HB_AGE=10" in fp(interval=4.0)[1][-1], "the suggestion names the 2.5 x interval value"
        assert any(l.strip().startswith("O0_FLEET_MAX_HB_AGE=9.0 outside") for l in fp(max_age=9.0)[1]), "the age rule names its variable"
        assert not any("O0_FLEET_MAX_HB_JUMP" in l for l in fp(max_age=9.0)[1][:-1]), "only the age is out of band"
        checks += 5
        # control-plane unit isolation (review wac-032-r2 🟡-6, site check S-10 as a gate)
        cp = base / "cp"
        (cp / "api").mkdir(parents=True)
        oq_env_f, nc_env_f, bad_env_f = base / "operator-query.env", base / "node-control.env", base / "leaky.env"
        oq_env_f.write_text("WATCHER_SNAPSHOT_TOKEN=SENTINELoqSN0123456789\nRISK_ADMIN_TOKEN=x\n", encoding="utf-8")   # WGW-1.0.4: no gateway token
        nc_env_f.write_text("NAUTILUS_NODE_AUTH_JSON={}\n", encoding="utf-8")
        bad_env_f.write_text("export WATCHER_SNAPSHOT_TOKEN=SENTINELleak0123456789\n", encoding="utf-8")
        U_OQ, U_NC, U_EI = "trader-v3-controlplane-operator-query", "trader-v3-controlplane-node-control", "trader-v3-controlplane-event-ingest"
        def show(wd, files, env="", reload="no", role=None):
            role = "node-control" if role is None else role     # any explicit non-'all' role satisfies RS-17's role rule
            return "LoadState=loaded\nNeedDaemonReload=%s\nFragmentPath=/etc/systemd/system/x.service\nWorkingDirectory=%s\n%sEnvironment=%s\n" % (
                reload, wd, "".join(f"EnvironmentFiles={f} (ignore_errors=no)\n" for f in files), (env + (" CONTROL_PLANE_APP_ROLE=" + role if role else "")).strip())
        good_shows = {U_OQ: show(cp / "api", [oq_env_f], "PYTHONPATH=/x", role="operator-query"), U_NC: show(cp / "api", [nc_env_f], role="node-control"),
                      U_EI: show(cp / "api", [], "FOO=SENTINELvalue0123456789 BAR=1", role="event-ingest")}
        def iso(shows):
            return cp_isolation_check(shows, U_OQ, [U_NC, U_EI], str(oq_env_f), str(cp))
        rc_i, lines_i = iso(good_shows)
        assert rc_i == 0 and f"CP_ISOLATION_OK units=3 watcher_gateway=absent shared_code_dir={U_OQ},{U_NC},{U_EI} d04=no" in lines_i[-1], lines_i
        for name, shows_bad, want in (
            ("node-control loads operator-query.env", dict(good_shows, **{U_NC: show(cp / "api", [nc_env_f, oq_env_f])}), 1),
            ("event-ingest Environment= carries a watcher token name", dict(good_shows, **{U_EI: show(cp / "api", [], "WATCHER_GATEWAY_TOKEN=SENTINELenv0123456789")}), 1),
            ("node-control env file defines a watcher token", dict(good_shows, **{U_NC: show(cp / "api", [bad_env_f])}), 1),
            ("operator-query runs from another directory", dict(good_shows, **{U_OQ: show(base / "elsewhere", [oq_env_f])}), 1),
            # systemd prints NeedDaemonReload=no for a unit that does not exist: only the LoadState rule can refuse it
            ("unknown unit (LoadState not-found)", dict(good_shows, **{U_EI: "LoadState=not-found\nNeedDaemonReload=no\n"}), 2),
            ("empty systemctl output", dict(good_shows, **{U_NC: ""}), 2),
            ("unreadable env file without ignore_errors", dict(good_shows, **{U_NC: show(cp / "api", [base / "missing.env"])}), 2),
        ):
            rc_i, lines_i = iso(shows_bad)
            assert rc_i == want, (name, rc_i, lines_i)
            text_i = "\n".join(lines_i)
            assert "SENTINEL" not in text_i, (name, "env value printed")
            checks += 1
        # WGW-1.0.4 RS-17: the three units keep the shared directory, an explicit non-'all' role, no gateway token anywhere
        rc_i, lines_i = iso(dict(good_shows, **{U_NC: show(base / "other", [nc_env_f], role="node-control"), U_EI: show(base / "other", [], role="event-ingest")}))
        assert rc_i == 1 and any(l.startswith("CODE_DIR_MISMATCH " + U_NC) for l in lines_i), lines_i
        for role in ("", "all", "watcher-gateway"):
            rc_i, lines_i = iso(dict(good_shows, **{U_EI: show(cp / "api", [], role=role)}))
            assert rc_i == 1 and any(l.startswith("ROLE_NOT_EXPLICIT " + U_EI) for l in lines_i), (role, lines_i)
        leaky_oq = base / "oq-with-gw.env"
        leaky_oq.write_text("WATCHER_GATEWAY_TOKEN=SENTINELoqGW0123456789\n", encoding="utf-8")
        rc_i, lines_i = cp_isolation_check(dict(good_shows, **{U_OQ: show(cp / "api", [leaky_oq], role="operator-query")}), U_OQ, [U_NC, U_EI], str(leaky_oq), str(cp))
        assert rc_i == 1 and any("VIOLATION " + U_OQ + ": defines WATCHER_GATEWAY_TOKEN" in l for l in lines_i) and "SENTINEL" not in "\n".join(lines_i), lines_i
        checks += 3
        rc_i, lines_i = iso(dict(good_shows, **{U_EI: "LoadState=loaded\nNeedDaemonReload=no\nWorkingDirectory=%s\nEnvironmentFiles=-%s\nEnvironment=CONTROL_PLANE_APP_ROLE=event-ingest\n" % (cp / "api", base / "missing.env")}))
        assert rc_i == 0, ("a missing '-' (optional) env file is not a violation", lines_i)
        checks += 2
        # review wac-072 🟡-3: unparseable EnvironmentFiles= and not-yet-loaded drop-ins are UNCOMPARABLE (rc 2), never OK
        spaced = base / "dir with space"
        spaced.mkdir()
        (spaced / "nc.env").write_text("export WATCHER_GATEWAY_TOKEN_PREVIOUS=SENTINELspace0123456789\n", encoding="utf-8")
        for name, shows_bad, want in (
            ("EnvironmentFiles= value without a leading '/'", dict(good_shows, **{U_NC: show(cp / "api", []) + "EnvironmentFiles=relative/nc.env (ignore_errors=no)\n"}), 2),
            ("EnvironmentFiles= with an unknown suffix", dict(good_shows, **{U_NC: show(cp / "api", []) + f"EnvironmentFiles={nc_env_f} (ignore_errors=maybe)\n"}), 2),
            ("bare path with a space (no suffix)", dict(good_shows, **{U_NC: show(cp / "api", []) + f"EnvironmentFiles={spaced}/nc.env\n"}), 2),
            ("NeedDaemonReload=yes (drop-in changed on disk, not loaded)", dict(good_shows, **{U_EI: show(cp / "api", [], reload="yes")}), 2),
            ("NeedDaemonReload missing (property not read)", dict(good_shows, **{U_EI: show(cp / "api", []).replace("NeedDaemonReload=no\n", "")}), 2),
            ("path with a space and the systemd suffix parses (and its watcher name is found)", dict(good_shows, **{U_NC: show(cp / "api", [spaced / "nc.env"])}), 1),
        ):
            rc_i, lines_i = iso(shows_bad)
            assert rc_i == want, (name, rc_i, lines_i)
            assert "SENTINEL" not in "\n".join(lines_i), (name, "env value printed")
            checks += 1
        rc_i, lines_i = iso(good_shows)
        assert any(l.startswith(U_NC + ": FragmentPath=/etc/systemd/system/x.service") for l in lines_i), ("unit file paths recorded", lines_i)
        # review wac-072 🟡-4: symlink to operator-query.env (realpath), systemd's "(ignore_errors=yes)" form for an
        # absent optional file, a file NAMED operator-query.env elsewhere (by name, present or not)
        oq_pre = base / "srv" / "operator-query.env"
        oq_pre.parent.mkdir()
        oq_pre.write_text("RISK_ADMIN_TOKEN=x\n", encoding="utf-8")          # before O-2: no watcher names yet
        link = base / "nc-link.env"
        link.symlink_to(oq_pre)
        rc_i, lines_i = cp_isolation_check(dict(good_shows, **{U_NC: show(cp / "api", [link], role="node-control")}), U_OQ, [U_NC, U_EI], str(oq_pre), str(cp))
        assert rc_i == 1 and any("ENVFILE_ISOLATION VIOLATION " + U_NC + " loads" in l for l in lines_i), ("symlink to operator-query.env", lines_i)
        rc_i, lines_i = iso(dict(good_shows, **{U_EI: show(cp / "api", []) + f"EnvironmentFiles={base}/absent-optional.env (ignore_errors=yes)\n"}))
        assert rc_i == 0 and any("absent (ignore_errors=yes)" in l for l in lines_i), ("systemd ignore_errors=yes form", lines_i)
        rc_i, lines_i = iso(dict(good_shows, **{U_EI: show(cp / "api", []) + "EnvironmentFiles=/elsewhere/operator-query.env (ignore_errors=yes)\n"}))
        assert rc_i == 1 and any("loads /elsewhere/operator-query.env" in l for l in lines_i), ("operator-query.env by name", lines_i)
        checks += 4
        wgw_checks = _selftest_wgw(base, cp, good_shows, show, U_OQ, U_NC, U_EI, oq_env_f)
        checks += wgw_checks
        # warm-up
        j = ("Sep 26 x uvicorn[11]: INFO snapshot_warmup result=success revision=7 content_sha256=abcdefabcdef pid=11 role=operator-query duration_ms=40\n"
             "Sep 26 x uvicorn[12]: INFO snapshot_warmup result=success revision=7 content_sha256=abcdefabcdef pid=12 role=operator-query duration_ms=41\n")
        assert warmup_check(j, 2, 7, "abcdefabcdef" + "0" * 52)[0]
        assert not warmup_check(j, 3, 7, None)[0], "missing worker accepted"
        assert not warmup_check("", 2, 7, None)[0], "no lines accepted"
        assert not warmup_check(j.replace("pid=12", "pid=12").replace("result=success revision=7 content_sha256=abcdefabcdef pid=12", "result=cold revision=None content_sha256=None pid=12"), 2, 7, None)[0], "cold worker accepted"
        assert not warmup_check(j, 2, 8, None)[0], "revision below R0 accepted"
        assert not warmup_check(j, 2, 7, "ffffffffffff")[0], "same revision different digest accepted"
        checks += 6
    finally:
        for path in sorted(base.rglob("*"), reverse=True):
            path.unlink() if (path.is_symlink() or path.is_file()) else path.rmdir()
        base.rmdir()
    print(f"SELFTEST_OK wgw_stage_o={wgw_checks} checks={checks} fleet_cases={len(cases)} redaction_shapes=3+2 path_shapes=4+{len(PATH_SHAPES_R3)} path_pins={len(PATH_RULE_PINS)} legit_paths={len(LEGIT_PATHS)} gates=14 fleet_params=12 cp_isolation=19 warmup=6")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("env-exec")
    p.add_argument("--env-file", type=Path, action="append")
    p.add_argument("--clear-env", action="store_true")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_env_exec)
    p = sub.add_parser("redact")
    p.set_defaults(func=cmd_redact)
    p = sub.add_parser("manifest-build")
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--paths-file", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.set_defaults(func=cmd_manifest_build)
    p = sub.add_parser("manifest-verify")
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--label")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_manifest_verify)
    p = sub.add_parser("sqlite-backup")
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--dest", type=Path, required=True)
    p.set_defaults(func=cmd_sqlite_backup)
    p = sub.add_parser("http-probe")
    p.add_argument("--url", required=True)
    p.add_argument("--method", default="GET")
    p.add_argument("--token-env-file", type=Path)
    p.add_argument("--token-var")
    p.add_argument("--proxy-header")
    p.add_argument("--bogus-bearer", action="store_true")
    p.add_argument("--header", action="append")
    p.add_argument("--show-header", action="append")
    p.add_argument("--show-json-field", action="append", help="top-level non-secret fields only (revision, content_sha256, ...)")
    p.add_argument("--expect-status", type=int, action="append")
    p.add_argument("--expect-code")
    p.add_argument("--expect-no-location", action="store_true")
    p.add_argument("--expect-json", action="append", help="KEY=VALUE on a non-secret top-level key (status, app_role, database, gateway)")
    p.add_argument("--timeout", type=float, default=10.0)
    p.set_defaults(func=cmd_http_probe)
    p = sub.add_parser("closure")
    p.add_argument("--watcher-root", type=Path, required=True)
    p.add_argument("--builder", type=Path, required=True)
    p.set_defaults(func=cmd_closure)
    p = sub.add_parser("sums-compare")
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--sums", type=Path, required=True)
    p.add_argument("--prefix", required=True)
    p.add_argument("--label")
    p.set_defaults(func=cmd_sums_compare)
    p = sub.add_parser("redact-json")
    p.set_defaults(func=cmd_redact_json)
    p = sub.add_parser("caddy-skeleton")
    p.set_defaults(func=cmd_caddy_skeleton)
    p = sub.add_parser("fleet-compare")
    p.add_argument("--before", type=Path, required=True)
    p.add_argument("--after", type=Path, help="omit to check the baseline only (complete and fresh)")
    p.add_argument("--nodes", required=True, help="expected node ids, space separated (from site check S-00)")
    p.add_argument("--ready-ports", default="8081 8082 8083 8084")
    p.add_argument("--max-hb-age", type=float, default=5.0, help="docs/agent-operations.md §0: heartbeat age should be < 5 s")
    p.add_argument("--max-hb-jump", type=float, default=5.0)
    p.add_argument("--known-down", default="", help="nodes the user declared stopped before the change")
    p.set_defaults(func=cmd_fleet_compare)
    p = sub.add_parser("fleet-params", help="guard thresholds vs the production node heartbeat parameters (S-00)")
    p.add_argument("--hb-interval-s", type=float, required=True)
    p.add_argument("--hb-timeout-s", type=float, required=True)
    p.add_argument("--max-hb-age", type=float, required=True)
    p.add_argument("--max-hb-jump", type=float, required=True)
    p.add_argument("--settle-s", type=float, required=True)
    p.add_argument("--samples", type=int, required=True)
    p.add_argument("--sample-interval-s", type=float, required=True)
    p.set_defaults(func=cmd_fleet_params)
    p = sub.add_parser("cp-isolation", help="stage O gate: control-plane unit env isolation (site check S-10)")
    p.add_argument("--oq-unit", required=True)
    p.add_argument("--other-unit", action="append", required=True)
    p.add_argument("--oq-env", required=True)
    p.add_argument("--cp-root", required=True)
    p.add_argument("--wgw-unit", help=f"RS-17: the watcher-gateway unit ({WGW_UNIT})")
    p.add_argument("--wgw-env", help="its env file (secrets/control-plane/watcher-gateway.env)")
    p.add_argument("--wgw-root", help="releases/watcher-gateway (its WorkingDirectory must be below)")
    p.add_argument("--wgw-may-be-absent", action="store_true", help="preflight: the unit is not installed yet")
    p.add_argument("--allow-auth-secret-key", action="store_true", help="U-13 (iii) approved: AUTH_SECRET_KEY may be in the watcher-gateway env")
    p.set_defaults(func=cmd_cp_isolation)
    p = sub.add_parser("wgw-env", help="stage O: build (or --check) the watcher-gateway env file from the whitelist (names only printed)")
    p.add_argument("--oq-env", type=Path, help="the live operator-query.env (source of the four reader tokens)")
    p.add_argument("--gateway-fragment", type=Path, help="credential set watcher-gateway.env (WATCHER_GATEWAY_TOKEN)")
    p.add_argument("--out", type=Path, help="new file (0600, never overwritten)")
    p.add_argument("--set-optional", action="append", help="NAME=VALUE for WATCHER_GATEWAY_URL / _CONFIG_SLOTS / _MEDIA_SLOTS")
    p.add_argument("--with-auth-secret-key", action="store_true", help="U-13 (iii) only: also copy AUTH_SECRET_KEY")
    p.add_argument("--check", type=Path, help="validate an existing watcher-gateway env file instead")
    p.set_defaults(func=cmd_wgw_env)
    p = sub.add_parser("shared-tree", help="stage O: shared control-plane code dir vs the 67b401a manifest; before/after snapshot")
    p.add_argument("--root", type=Path, required=True, help="TRADER_ROOT (/srv/trader-v3)")
    p.add_argument("--manifest", type=Path, help="bundle cp-shared.baseline.sha256")
    p.add_argument("--sub", action="append", help=f"sub-directories (default {' '.join(SHARED_SUBDIRS)})")
    p.add_argument("--accept-drift-sha256", help="the user's decision for exactly this drift list (see the SUGGESTION lines)")
    p.add_argument("--snapshot-out", type=Path, help="write the full snapshot (every entry: sha256, mode, owner)")
    p.add_argument("--compare", type=Path, help="compare the directory now with an earlier snapshot")
    p.set_defaults(func=cmd_shared_tree)
    p = sub.add_parser("daemon-reload-check", help="stage O: every unit NeedDaemonReload=no (else DAEMON_RELOAD_PENDING)")
    p.add_argument("--show", type=Path, required=True, help="output of `systemctl show -p Id -p NeedDaemonReload <every unit>`")
    p.add_argument("--exclude", action="append", help="a unit this very step installed (never loaded yet)")
    p.add_argument("--require", action="append", help="a unit that must be in the list (else UNCOMPARABLE)")
    p.set_defaults(func=cmd_daemon_reload_check)
    p = sub.add_parser("port-check", help="`ss -H -ltnp` output: a port free, or listening on loopback only")
    p.add_argument("--ss-file", type=Path, required=True)
    p.add_argument("--port", default=WGW_PORT)
    p.add_argument("--expect", choices=("free", "loopback-only"), required=True)
    p.add_argument("--record-range", help="e.g. 8184-8189: record every listener in the range (PC-6 (ii))")
    p.set_defaults(func=cmd_port_check)
    p = sub.add_parser("public-direct-check", help="V-5 (RS-19): five unauthenticated GETs, status codes only")
    p.add_argument("--host", required=True, help="the public host name")
    p.add_argument("--phase", choices=("unit-running", "before-unit"), required=True)
    p.add_argument("--loopback", action="store_true", help="jp-24 supplementary check (127.0.0.1 with the public name); never external evidence")
    p.add_argument("--scheme", choices=("https", "http"), default="https", help=argparse.SUPPRESS)
    p.add_argument("--port", type=int, default=443, help=argparse.SUPPRESS)
    p.add_argument("--timeout", type=float, default=10.0)
    p.set_defaults(func=cmd_public_direct_check)
    p = sub.add_parser("wgw-unit", help="render (--render) or lint (--lint) the watcher-gateway systemd unit")
    p.add_argument("--render", action="store_true")
    p.add_argument("--lint", type=Path)
    p.add_argument("--trader-root", default="/srv/trader-v3")
    p.add_argument("--release-sha", required=True)
    p.add_argument("--resource-conf", type=Path, help="infra/systemd/account-stall-control-plane-reader.conf of the candidate")
    p.add_argument("--out", type=Path)
    p.set_defaults(func=cmd_wgw_unit)
    p = sub.add_parser("wgw-smoke", help="RS-16 dependency check: python -B import smoke of the new code dir with the whitelisted env")
    p.add_argument("--code-dir", type=Path, required=True, help="the release tree (staging in preflight, the installed dir in apply)")
    p.add_argument("--venv", type=Path, required=True, help="the shared venv, read-only (/srv/trader-v3/.venv-cp)")
    p.add_argument("--env-file", type=Path, required=True, help="the watcher-gateway env (read in-process, values never printed)")
    p.add_argument("--trader-root", default="/srv/trader-v3")
    p.add_argument("--as-user", help=f"run as this user via setpriv (apply: {WGW_USER})")
    p.add_argument("--with-auth-secret-key", action="store_true")
    p.add_argument("--timeout", type=float, default=120.0)
    p.set_defaults(func=cmd_wgw_smoke)
    p = sub.add_parser("v5-evidence-check", help="O-3: the external V-5 evidence file (else DIRECT_GUARD_UNVERIFIED)")
    p.add_argument("--evidence", type=Path)
    p.add_argument("--host", required=True)
    p.add_argument("--phase", default="unit-running")
    p.add_argument("--not-before", type=int, default=0, help="epoch of the unit start: older evidence is refused")
    p.add_argument("--max-age-s", type=int, default=3600)
    p.set_defaults(func=cmd_v5_evidence_check)
    p = sub.add_parser("gate-write")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--stage", required=True)
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument("--field", action="append")
    p.add_argument("--file-sha", action="append", help="key=path: store sha256 of the file")
    p.set_defaults(func=cmd_gate_write)
    p = sub.add_parser("gate-check")
    p.add_argument("--gate", type=Path, required=True)
    p.add_argument("--stage", required=True)
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument("--max-age-s", type=int, default=3600)
    p.add_argument("--expect", action="append")
    p.add_argument("--expect-file-sha", action="append")
    p.set_defaults(func=cmd_gate_check)
    p = sub.add_parser("warmup-check")
    p.add_argument("--journal", type=Path, required=True)
    p.add_argument("--workers", type=int, required=True)
    p.add_argument("--min-revision", type=int, required=True)
    p.add_argument("--r0-content-sha256")
    p.set_defaults(func=cmd_warmup_check)
    p = sub.add_parser("selftest")
    p.set_defaults(func=cmd_selftest)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
