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
  redact-json      stdin JSON -> stdout JSON with basic-auth passwords and literal
                   header values replaced (placeholders like {env.X} are kept)
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
)
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


def _redact_json(node):
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key == "password" and isinstance(value, str):
                out[key] = "<redacted>"
            elif key in ("set", "add") and isinstance(value, dict):
                out[key] = {
                    name: [v if isinstance(v, str) and re.fullmatch(r"(Bearer )?\{[A-Za-z0-9_.$]+\}", v) else f"<literal len={len(str(v))}>" for v in (vals or [])]
                    for name, vals in value.items()
                }
            else:
                out[key] = _redact_json(value)
        return out
    if isinstance(node, list):
        return [_redact_json(v) for v in node]
    return node


def cmd_redact_json(_args: argparse.Namespace) -> int:
    json.dump(_redact_json(json.load(sys.stdin)), sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
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
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
