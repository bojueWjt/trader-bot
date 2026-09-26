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


def redact_line(line: str) -> str:
    if line.lstrip().startswith("Environment="):
        names = [a.split("=", 1)[0].strip('"') for a in line.split("=", 1)[1].split()]
        return "Environment=<names:" + ",".join(names) + ">"
    match = KV.match(line)
    if match and SECRET_KEY_RE.search(match.group(2)) and match.group(4).strip() and not match.group(4).strip().startswith("{"):
        line = f"{match.group(1)}{match.group(2)}{match.group(3)}<redacted len={len(match.group(4).strip())}>"
    for pattern, repl in REDACTIONS:
        line = pattern.sub(repl, line)
    return LONG_OPAQUE.sub("<redacted-opaque>", line)


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
    try:
        parsed = json.loads(body.decode("utf-8"))
        if isinstance(parsed, dict):
            code = str(parsed.get("code") or parsed.get("detail") or "")[:60]
            allowed = {"revision", "content_sha256", "schema_version", "generated_at", "snapshot_state", "observed_at", "connection", "listener"}
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
        return red if red == node else _literal(node)
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
        failed = json.loads(gate.read_text())
        failed["ok"] = False
        gate.write_text(json.dumps(failed))
        assert not gate_check(gate, "caddy-preflight", bundle, 3600, exp)[0], "failed gate accepted"
        (bundle / "RELEASE.json").write_text(json.dumps({"candidate": "c" * 40, "deploy_candidate": False}), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            assert cmd_gate_write(argparse.Namespace(bundle=bundle, stage="s", out=base / "g2.json", field=[], file_sha=[])) == 1, "gate written for a non-candidate"
        checks += 10
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
            path.unlink() if path.is_file() else path.rmdir()
        base.rmdir()
    print(f"SELFTEST_OK checks={checks} fleet_cases={len(cases)} redaction_shapes=3+2 gates=10 warmup=6")
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
