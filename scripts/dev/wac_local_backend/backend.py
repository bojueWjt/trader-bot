#!/usr/bin/env python3
"""wac-100 local test backend: watcher + control-plane watcher gateway on loopback.

Subcommands: up, down, status, verify, token.

Safety properties (each one is load-bearing, keep them):
* Children get a scrubbed environment built from scratch; nothing from the
  caller's environment is inherited except PATH entries for node/python.
  No real env/secret file is ever read.
* All tokens are fresh random test values, written to a 0600 env file inside a
  0700 state directory. They are never written to logs or printed unless
  ``token --reveal`` is used.
* The watcher runs from a copy of its code inside the state directory, so its
  ``config.json`` / ``messages.json`` (which live next to server.js) are fresh
  and empty: no Telegram session, no apiId, no outbound Telegram traffic.
  Telegram traffic is additionally pointed at a closed loopback SOCKS port,
  the price monitor and signal importer are disabled, and no alert bot token is set.
* Both servers bind 127.0.0.1 only. The script never runs adb.
* ``down`` only signals processes whose start time and command line match what
  ``up`` recorded (the command line always contains the state directory), then
  removes the state directory, which is identified by a marker file.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import seed  # noqa: E402

REPO = HERE.parents[2]
WATCHER_SRC = REPO / "bridge/services/telegram-watcher"
API_DIR = REPO / "services/control-plane/api"
LAUNCHER = HERE / "gateway_launcher.py"

HOST = "127.0.0.1"
WATCHER_PORT = 9100  # hard-coded in bridge/services/telegram-watcher/server.js
DEFAULT_PORT = 18731
DEFAULT_ROLE = "operator-query"
MARKER = ".wac-local-backend"
ENV_NAME = "test.env"
TELEGRAM_BLACKHOLE_PORT = 9  # discard port, nothing listens: any accidental MTProto dial fails locally

READER_TOKENS = {
    "risk_admin": "RISK_ADMIN_TOKEN",
    "viewer": "VIEWER_TOKEN",
    "reviewer": "REVIEWER_TOKEN",
    "system_observer": "SYSTEM_OBSERVER_TOKEN",
}
WATCHER_TOKENS = ("WATCHER_GATEWAY_TOKEN", "WATCHER_SNAPSHOT_TOKEN", "WATCHER_BROWSER_PROXY_TOKEN")
ALL_TOKENS = tuple(READER_TOKENS.values()) + WATCHER_TOKENS
TOKEN_RE = re.compile(r"^[\x21-\x7E]{32,}$")
ENV_LINE_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=([A-Za-z0-9_.:/-]*)$")
WATCHER_COPY = ("server.js", "price-monitor.js", "package.json", "lib", "public")


class BackendError(RuntimeError):
    pass


# --------------------------------------------------------------------------- state dir


def default_state_dir(port: int) -> Path:
    return Path(tempfile.gettempdir()).resolve() / f"wac-local-backend-{port}"


def init_state_dir(state_dir: Path, meta: dict) -> Path:
    state_dir = Path(state_dir)
    if state_dir.exists():
        raise BackendError(f"state dir already exists (backend running or stale): {state_dir}; run down first")
    state_dir.mkdir(mode=0o700, parents=False)
    os.chmod(state_dir, 0o700)
    for name in ("secrets", "run", "logs", "data", "media", "home", "tmp"):
        (state_dir / name).mkdir(mode=0o700)
    (state_dir / MARKER).write_text(json.dumps({**meta, "state_dir": str(state_dir)}), encoding="utf-8")
    return state_dir


def read_marker(state_dir: Path) -> dict:
    marker = Path(state_dir) / MARKER
    if not marker.is_file() or marker.is_symlink():
        raise BackendError(f"not a wac local backend state dir (marker missing): {state_dir}")
    meta = json.loads(marker.read_text(encoding="utf-8"))
    if Path(meta.get("state_dir", "")) != Path(state_dir):
        raise BackendError("state dir marker does not match its location")
    return meta


# --------------------------------------------------------------------------- tokens / env file


def generate_tokens() -> dict[str, str]:
    tokens: dict[str, str] = {}
    while len(set(tokens.values())) != len(ALL_TOKENS):
        tokens = {name: "wactest_" + secrets.token_urlsafe(32) for name in ALL_TOKENS}
    for value in tokens.values():
        if not TOKEN_RE.fullmatch(value):
            raise BackendError("generated token failed the watcher token format")
    return tokens


def write_env_file(path: Path, values: dict[str, str]) -> Path:
    path = Path(path)
    lines = []
    for key, value in values.items():
        line = f"{key}={value}"
        if not ENV_LINE_RE.fullmatch(line):
            raise BackendError(f"refusing to write unsafe env line for {key}")
        lines.append(line)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, ("\n".join(lines) + "\n").encode("ascii"))
    finally:
        os.close(fd)
    return path


def read_env_file(path: Path) -> dict[str, str]:
    path = Path(path)
    info = os.lstat(path)
    if not os.path.isfile(path) or os.path.islink(path):
        raise BackendError(f"env file is not a regular file: {path}")
    if info.st_mode & 0o077:
        raise BackendError(f"env file permissions too open ({oct(info.st_mode & 0o777)}): {path}")
    if info.st_uid != os.getuid():
        raise BackendError(f"env file not owned by current user: {path}")
    values = {}
    for line in path.read_text(encoding="ascii").splitlines():
        if not line:
            continue
        match = ENV_LINE_RE.fullmatch(line)
        if not match:
            raise BackendError("malformed env file line")
        values[match.group(1)] = match.group(2)
    return values


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- child environments


def _base_env(state_dir: Path, executables: list[str]) -> dict[str, str]:
    path_entries = []
    for executable in executables:
        parent = str(Path(executable).parent)
        if parent not in path_entries:
            path_entries.append(parent)
    path_entries += ["/usr/bin", "/bin"]
    return {
        "PATH": ":".join(path_entries),
        "HOME": str(state_dir / "home"),
        "TMPDIR": str(state_dir / "tmp"),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
    }


def watcher_env(state_dir: Path, tokens: dict[str, str], node: str) -> dict[str, str]:
    db = str(state_dir / "data/watcher-trading.db")
    env = _base_env(state_dir, [node])
    env.update({
        "WATCHER_HOST": HOST,
        "WATCHER_MEDIA_DIR": str(state_dir / "media"),
        "TRADER_TRADING_DB_PATH": db,
        "TRADING_DB_PATH": db,
        "PRICE_MONITOR_ENABLED": "0",
        "SIGNAL_IMPORTER_ENABLED": "0",
        "TELEGRAM_PROXY_ENABLED": "1",
        "TELEGRAM_PROXY_HOST": HOST,
        "TELEGRAM_PROXY_PORT": str(TELEGRAM_BLACKHOLE_PORT),
        "TELEGRAM_CONNECTION_RETRIES": "1",
        "NODE_ENV": "development",
    })
    for name in WATCHER_TOKENS:
        env[name] = tokens[name]
    return env


def gateway_env(state_dir: Path, tokens: dict[str, str], python: str, role: str) -> dict[str, str]:
    env = _base_env(state_dir, [python])
    env.update({
        "CONTROL_PLANE_APP_ROLE": role,
        "WATCHER_GATEWAY_URL": f"http://{HOST}:{WATCHER_PORT}",
        "WATCHER_GATEWAY_TOKEN": tokens["WATCHER_GATEWAY_TOKEN"],
        "WATCHER_CONFIG_SNAPSHOT_ENABLED": "0",
        "TRADER_TRADING_DB_PATH": str(state_dir / "data/watcher-trading.db"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    })
    for name in READER_TOKENS.values():
        env[name] = tokens[name]
    return env


# --------------------------------------------------------------------------- tool discovery


def _main_checkout() -> Path:
    try:
        common = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--git-common-dir"], capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return REPO
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = (REPO / common_path).resolve()
    return common_path.parent


def _probe(cmd: list[str], cwd: Path | None = None) -> bool:
    try:
        return subprocess.run(cmd, cwd=cwd, env={"PATH": "/usr/bin:/bin", "HOME": tempfile.gettempdir()}, capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def find_python(explicit: str | None) -> str:
    probe = "import uvicorn, fastapi, httpx, psycopg2"
    candidates = [explicit] if explicit else []
    main = _main_checkout()
    for root in dict.fromkeys([REPO, main]):
        candidates += [str(root / ".venv-arch/bin/python"), str(root / "bridge/.venv/bin/python")]
    for candidate in candidates:
        if candidate and Path(candidate).exists() and _probe([candidate, "-c", probe]):
            return candidate
    raise BackendError("no python with uvicorn+fastapi+httpx+psycopg2 found; pass --python")


def find_node(explicit: str | None) -> str:
    node = explicit or shutil.which("node")
    if not node:
        raise BackendError("node not found; pass --node")
    return str(Path(node).resolve())


def find_node_modules(explicit: str | None) -> Path:
    candidates = [Path(explicit)] if explicit else []
    main = _main_checkout()
    candidates += [WATCHER_SRC / "node_modules", main / "bridge/services/telegram-watcher/node_modules"]
    for candidate in candidates:
        if (candidate / "better-sqlite3").is_dir() and (candidate / "express").is_dir():
            return candidate.resolve()
    raise BackendError("watcher node_modules not found; run npm ci in bridge/services/telegram-watcher or pass --node-modules")


# --------------------------------------------------------------------------- ports / processes


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((HOST, port)) == 0


def _ps(*args: str) -> str:
    result = subprocess.run(["ps", *args], capture_output=True, text=True, timeout=10)
    return result.stdout.strip() if result.returncode == 0 else ""


def process_start(pid: int) -> str:
    return _ps("-o", "lstart=", "-p", str(pid))


def process_command(pid: int) -> str:
    return _ps("-ww", "-o", "command=", "-p", str(pid))


def _reap(pid: int) -> None:
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass


def pid_alive(pid: int) -> bool:
    _reap(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    state = _ps("-o", "stat=", "-p", str(pid))
    return bool(state) and not state.startswith("Z")


def record_process(state_dir: Path, name: str, proc: subprocess.Popen) -> dict:
    entry = {"name": name, "pid": proc.pid, "pgid": os.getpgid(proc.pid), "start": process_start(proc.pid)}
    path = Path(state_dir) / "run/processes.json"
    entries = json.loads(path.read_text()) if path.exists() else []
    entries.append(entry)
    path.write_text(json.dumps(entries, indent=1))
    return entry


def _group_members(pgid: int, marker: str) -> list[int]:
    members = []
    for line in _ps("-ww", "-A", "-o", "pid=,pgid=,command=").splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[1] == str(pgid) and marker in parts[2]:
            members.append(int(parts[0]))
    return members


def owned_pids(state_dir: Path, entry: dict) -> list[int]:
    """Pids that provably belong to this backend: recorded identity + state dir on the command line."""
    marker = str(state_dir)
    pids = []
    pid = int(entry["pid"])
    if pid_alive(pid) and process_start(pid) == entry["start"] and marker in process_command(pid):
        pids.append(pid)
    for member in _group_members(int(entry["pgid"]), marker):
        if member not in pids and pid_alive(member):
            pids.append(member)
    return pids


def stop_processes(state_dir: Path, grace_s: float = 10.0, log=print) -> list[dict]:
    path = Path(state_dir) / "run/processes.json"
    entries = json.loads(path.read_text()) if path.exists() else []
    report = []
    for entry in reversed(entries):
        pids = owned_pids(state_dir, entry)
        if not pids:
            if pid_alive(int(entry["pid"])):
                log(f"  {entry['name']}: pid {entry['pid']} is alive but not ours (identity mismatch); left untouched")
                report.append({**entry, "result": "foreign"})
            else:
                report.append({**entry, "result": "already_stopped"})
            continue
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline and any(pid_alive(pid) for pid in pids):
            time.sleep(0.1)
        survivors = [pid for pid in pids if pid_alive(pid)]
        for pid in survivors:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(pid_alive(pid) for pid in survivors):
            time.sleep(0.1)
        left = [pid for pid in pids if pid_alive(pid)]
        result = "stopped" if not left else "still_running"
        log(f"  {entry['name']}: {result} (pids {pids}{', SIGKILL ' + str(survivors) if survivors else ''})")
        report.append({**entry, "result": result, "pids": pids})
    return report


def teardown(state_dir: Path, log=print) -> bool:
    state_dir = Path(state_dir)
    read_marker(state_dir)
    report = stop_processes(state_dir, log=log)
    if any(item["result"] == "still_running" for item in report):
        log(f"  processes still running; state dir kept: {state_dir}")
        return False
    shutil.rmtree(state_dir)
    log(f"  removed {state_dir}")
    return True


# --------------------------------------------------------------------------- http


def http_request(port: int, method: str, path: str, token: str | None = None, headers: dict | None = None, timeout: float = 15.0):
    conn = http.client.HTTPConnection(HOST, port, timeout=timeout)
    try:
        all_headers = dict(headers or {})
        if token is not None:
            all_headers["Authorization"] = "Bearer " + token
        conn.request(method, path, headers=all_headers)
        response = conn.getresponse()
        body = response.read()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, body
    finally:
        conn.close()


def wait_for_port(port: int, proc: subprocess.Popen, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise BackendError(f"process exited early with code {proc.returncode}")
        if port_in_use(port):
            return
        time.sleep(0.2)
    raise BackendError(f"timed out waiting for 127.0.0.1:{port}")


def log_tail(path: Path, tokens: dict[str, str], lines: int = 30) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(no log)"
    for value in tokens.values():
        text = text.replace(value, "<redacted>")
    return "\n".join(text.splitlines()[-lines:])


def logs_leak_tokens(state_dir: Path, tokens: dict[str, str]) -> list[str]:
    """Secret token values found in log files (non-secret env entries such as WAC_BASE_URL are ignored)."""
    leaks = []
    for log in sorted((Path(state_dir) / "logs").glob("*.log")):
        text = log.read_text(encoding="utf-8", errors="replace")
        for name, value in tokens.items():
            if name in ALL_TOKENS and value in text:
                leaks.append(f"{log.name}:{name}")
    return leaks


# --------------------------------------------------------------------------- up


def copy_watcher(dest: Path, node_modules: Path) -> Path:
    dest.mkdir(mode=0o700)
    for name in WATCHER_COPY:
        source = WATCHER_SRC / name
        if source.is_dir():
            shutil.copytree(source, dest / name, ignore=shutil.ignore_patterns("node_modules", "__pycache__"))
        else:
            shutil.copy2(source, dest / name)
    os.symlink(node_modules, dest / "node_modules")
    return dest


def render_instructions(meta: dict, env_path: Path, manifest: dict | None) -> str:
    port = meta["port"]
    wrapper = HERE / "wac_local_backend.sh"
    lines = [
        "",
        "wac local test backend is up (synthetic data, random test tokens, loopback only)",
        f"  role            : {meta['role']}",
        f"  gateway         : http://{HOST}:{port}  (watcher gateway under /v1/watcher/*)",
        f"  watcher         : {HOST}:{WATCHER_PORT}  (internal upstream, not for the app)",
        f"  state dir       : {meta['state_dir']}",
        f"  env file (0600) : {env_path}",
    ]
    if manifest:
        lines.append(
            f"  data            : {manifest['messages_total']} messages ({manifest['messages_in_window']} in 24h), "
            f"{manifest['briefings_total']} briefings, {manifest['orders_total']} orders, {len(manifest['media'])} media files"
        )
    lines += [
        "",
        "Test package connection settings (Trading 配置 form; watcher API reuses it):",
        f"  控制面地址 baseUrl : http://{HOST}:{port}      (no /m suffix, no trailing slash)",
        "  operator token     : VIEWER_TOKEN from the env file (read-only role);",
        "                       RISK_ADMIN_TOKEN only if you need to test watcher writes",
        f"  show on this terminal only : {wrapper} token --port {port} --role viewer --reveal",
        "  The token is never written to any log by this script.",
        "",
        "adb (run it yourself; this script never runs adb):",
        f"  adb -s <serial> reverse tcp:{port} tcp:{port}",
        f"  undo: adb -s <serial> reverse --remove tcp:{port}",
        "",
        "Notes:",
        "  - The app must be a build that permits cleartext HTTP to 127.0.0.1 (usesCleartextTraffic).",
        "  - Only /v1/watcher/* is backed here; other /v1 endpoints need PostgreSQL and answer 503.",
        "  - Watcher status shows needs_login: there is deliberately no Telegram session.",
        "",
        f"Smoke check : {wrapper} verify --port {port}",
        f"Stop        : {wrapper} down --port {port}",
    ]
    return "\n".join(lines)


def spawn(cmd: list[str], cwd: Path, env: dict[str, str], log_path: Path) -> subprocess.Popen:
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        return subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=fd, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    finally:
        os.close(fd)


def cmd_up(args) -> int:
    state_dir = _resolve_state(args)
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,40}", args.role):
        raise BackendError("invalid --role")
    for port, label in ((args.port, "gateway"), (WATCHER_PORT, "watcher (hard-coded in server.js)")):
        if port_in_use(port):
            raise BackendError(f"127.0.0.1:{port} already in use ({label}); not touching the existing listener")
    python = find_python(args.python)
    node = find_node(args.node)
    node_modules = find_node_modules(args.node_modules)

    meta = {"port": args.port, "role": args.role, "watcher_port": WATCHER_PORT, "app": args.app, "python": python, "node": node}
    init_state_dir(state_dir, meta)
    meta = read_marker(state_dir)
    tokens = generate_tokens()
    try:
        env_path = write_env_file(state_dir / "secrets" / ENV_NAME, {
            **tokens,
            "WAC_BASE_URL": f"http://{HOST}:{args.port}",
            "WAC_ROLE": args.role,
        })
        app_dir = copy_watcher(state_dir / "watcher-app", node_modules)
        (app_dir / "config.json").write_text(json.dumps({"watchGroups": [c["channel_id"] for c in seed.CHANNELS]}, indent=2))
        if not _probe([node, "-e", "new (require('better-sqlite3'))(':memory:').close(); require('express')"], cwd=app_dir):
            raise BackendError("watcher node_modules do not load with this node (native module mismatch?)")
        print("seeding synthetic data ...", flush=True)
        result = seed.generate(state_dir / "data/watcher-trading.db", state_dir / "media", spec=seed.SeedSpec(seed=args.seed))
        seed.write_manifest(result, state_dir / "seed-manifest.json")
        manifest = json.loads((state_dir / "seed-manifest.json").read_text(encoding="utf-8"))

        watcher = spawn([node, str(app_dir / "server.js")], app_dir, watcher_env(state_dir, tokens, node), state_dir / "logs/watcher.log")
        record_process(state_dir, "watcher", watcher)
        wait_for_port(WATCHER_PORT, watcher, 30)
        print(f"watcher listening on {HOST}:{WATCHER_PORT}", flush=True)

        gateway = spawn(
            [python, str(LAUNCHER), "--state-dir", str(state_dir), "--app-dir", str(API_DIR), "--app", args.app, "--port", str(args.port)],
            state_dir / "home", gateway_env(state_dir, tokens, python, args.role), state_dir / "logs/gateway.log",
        )
        record_process(state_dir, "gateway", gateway)
        wait_for_port(args.port, gateway, 60)
        status, _, _ = http_request(args.port, "GET", "/v1/watcher/status", tokens["VIEWER_TOKEN"])
        if status != 200:
            raise BackendError(f"gateway smoke check: /v1/watcher/status with viewer token returned {status}")
        status, _, _ = http_request(args.port, "GET", "/v1/watcher/status")
        if status != 401:
            raise BackendError(f"gateway smoke check: /v1/watcher/status without token returned {status}")
        leaks = logs_leak_tokens(state_dir, tokens)
        if leaks:
            raise BackendError(f"token value found in logs: {leaks}")
    except BaseException as exc:
        print(f"up failed: {exc}", file=sys.stderr)
        for name in ("watcher", "gateway"):
            print(f"--- {name}.log (tail, tokens redacted)\n{log_tail(state_dir / 'logs' / f'{name}.log', tokens)}", file=sys.stderr)
        if args.keep_on_failure:
            print(f"state dir kept for inspection: {state_dir} (run down to clean up)", file=sys.stderr)
        else:
            teardown(state_dir, log=lambda line: print(line, file=sys.stderr))
        return 1
    print(render_instructions(meta, env_path, manifest))
    return 0


# --------------------------------------------------------------------------- down / status / token


def _resolve_state(args) -> Path:
    return (Path(args.state_dir) if args.state_dir else default_state_dir(args.port)).resolve()


def cmd_down(args) -> int:
    state_dir = _resolve_state(args)
    if not state_dir.exists():
        print(f"nothing to do: {state_dir} does not exist")
        return 0
    print(f"stopping wac local backend in {state_dir}")
    ok = teardown(state_dir)
    for port in (args.port, WATCHER_PORT):
        if port_in_use(port):
            print(f"  warning: 127.0.0.1:{port} still has a listener (not ours or not yet released)")
    return 0 if ok else 1


def cmd_status(args) -> int:
    state_dir = _resolve_state(args)
    if not state_dir.exists():
        print(f"not running (no state dir {state_dir})")
        return 3
    meta = read_marker(state_dir)
    print(f"state dir : {state_dir}\nrole      : {meta['role']}\ngateway   : http://{HOST}:{meta['port']}\nwatcher   : {HOST}:{WATCHER_PORT}")
    path = state_dir / "run/processes.json"
    healthy = True
    for entry in json.loads(path.read_text()) if path.exists() else []:
        pids = owned_pids(state_dir, entry)
        healthy &= bool(pids)
        print(f"{entry['name']:<9} : {'running pids ' + str(pids) if pids else 'NOT RUNNING'}")
    tokens = read_env_file(state_dir / "secrets" / ENV_NAME)
    try:
        status, _, body = http_request(meta["port"], "GET", "/v1/watcher/status", tokens["VIEWER_TOKEN"], timeout=5)
        detail = json.loads(body) if status == 200 else {}
        print(f"/v1/watcher/status (viewer) : {status} connection={detail.get('connection')} last_message_ingested_at={detail.get('last_message_ingested_at')}")
        healthy &= status == 200
    except OSError as exc:
        print(f"/v1/watcher/status : unreachable ({type(exc).__name__})")
        healthy = False
    return 0 if healthy else 1


def cmd_token(args) -> int:
    state_dir = _resolve_state(args)
    read_marker(state_dir)
    tokens = read_env_file(state_dir / "secrets" / ENV_NAME)
    value = tokens[READER_TOKENS[args.role]]
    if not args.reveal:
        print(f"{READER_TOKENS[args.role]} fingerprint {token_fingerprint(value)} (add --reveal to print the value on this terminal)")
        return 0
    print(value)
    return 0


# --------------------------------------------------------------------------- verify


class Checks:
    def __init__(self):
        self.failures = 0

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{' -- ' + detail if detail else ''}")
        self.failures += 0 if ok else 1
        return ok


def page_all(port: int, token: str, path: str, params: dict, limit: int) -> tuple[list[dict], int]:
    rows, pages, cursor = [], 0, None
    while True:
        query = dict(params, limit=limit)
        if cursor:
            query.update(before_created_at=cursor[0], before_id=cursor[1])
        status, _, body = http_request(port, "GET", f"{path}?{urlencode(query)}", token)
        if status != 200:
            raise BackendError(f"{path} page {pages + 1} returned {status}: {body[:200]!r}")
        page = json.loads(body)
        pages += 1
        rows.extend(page)
        if len(page) < limit:
            return rows, pages
        cursor = (page[-1]["created_at"], page[-1]["id"])
        if pages > 1000:
            raise BackendError("pagination did not terminate")


def _ordered_desc(rows: list[dict]) -> bool:
    keys = [(row["created_at"], row["id"]) for row in rows]
    return all(a > b for a, b in zip(keys, keys[1:]))


def cmd_verify(args) -> int:
    state_dir = _resolve_state(args)
    meta = read_marker(state_dir)
    port = meta["port"]
    tokens = read_env_file(state_dir / "secrets" / ENV_NAME)
    manifest = json.loads((state_dir / "seed-manifest.json").read_text(encoding="utf-8"))
    viewer = tokens["VIEWER_TOKEN"]
    c = Checks()

    status, _, body = http_request(port, "GET", "/v1/watcher/status", viewer)
    c.check("status with viewer token -> 200", status == 200, f"got {status}")
    status, _, body = http_request(port, "GET", "/v1/watcher/status")
    code = json.loads(body).get("code") if body else None
    c.check("status without token -> 401 unauthenticated", status == 401 and code == "unauthenticated", f"got {status} {code}")
    status, _, body = http_request(port, "GET", "/v1/watcher/status", "wactest_not_a_configured_token_0123456789")
    code = json.loads(body).get("code") if body else None
    c.check("status with unknown token -> 403 invalid_token", status == 403 and code == "invalid_token", f"got {status} {code}")
    for role, name in READER_TOKENS.items():
        status, _, _ = http_request(port, "GET", "/v1/watcher/status", tokens[name])
        c.check(f"status with {role} token -> 200", status == 200, f"got {status}")

    expected = manifest["messages_in_window"]
    for limit in (500, 97):
        rows, pages = page_all(port, viewer, "/v1/watcher/trading/messages", {"hours": 24}, limit)
        ids = [row["id"] for row in rows]
        c.check(f"messages hours=24 limit={limit}: all in-window rows exactly once", len(ids) == expected and len(set(ids)) == len(ids),
                f"{len(ids)} rows / {len(set(ids))} unique / expected {expected} over {pages} pages")
        c.check(f"messages hours=24 limit={limit}: strictly ordered by (created_at, id) desc", _ordered_desc(rows))
    rows, pages = page_all(port, viewer, "/v1/watcher/trading/messages", {"hours": 168}, 500)
    c.check("messages hours=168 covers out-of-window rows too", len(rows) == manifest["messages_total"], f"{len(rows)} vs {manifest['messages_total']}")
    for channel_id, count in manifest["messages_in_window_by_channel"].items():
        rows, _ = page_all(port, viewer, "/v1/watcher/trading/messages", {"hours": 24, "channel": channel_id}, 500)
        c.check(f"messages channel={channel_id}", len(rows) == count and all(r["channel_id"] == channel_id for r in rows), f"{len(rows)} vs {count}")
    rows, pages = page_all(port, viewer, "/v1/watcher/trading/briefings", {"hours": 24}, 50)
    c.check("briefings hours=24 limit=50 paged exactly once", len(rows) == manifest["briefings_in_window"] and len({r["id"] for r in rows}) == len(rows) and _ordered_desc(rows),
            f"{len(rows)} vs {manifest['briefings_in_window']} over {pages} pages")
    status, _, body = http_request(port, "GET", "/v1/watcher/trading/orders", viewer)
    c.check("orders -> 200", status == 200 and len(json.loads(body)) == manifest["orders_total"], f"got {status}")

    by_tier = {}
    for item in manifest["media"]:
        by_tier.setdefault(item["tier"], []).append(item)
    for tier in ("small", "large", "huge"):
        for item in by_tier.get(tier, [])[:2]:
            status, headers, body = http_request(port, "GET", f"/v1/watcher/media/{item['filename']}", viewer, timeout=60)
            c.check(f"media {tier} {item['size']} bytes -> 200 full body", status == 200 and len(body) == item["size"] and body[:8] == b"\x89PNG\r\n\x1a\n"
                    and headers.get("content-type") == "image/png", f"got {status} len={len(body)}")
    item = by_tier["small"][0]
    status, headers, body = http_request(port, "HEAD", f"/v1/watcher/media/{item['filename']}", viewer)
    c.check("media HEAD -> 200, no body, full length", status == 200 and not body and headers.get("content-length") == str(item["size"]), f"got {status}")
    status, headers, body = http_request(port, "GET", f"/v1/watcher/media/{item['filename']}", viewer, {"Range": "bytes=0-0"})
    c.check("media Range bytes=0-0 -> 206 with real total", status == 206 and len(body) == 1 and headers.get("content-range") == f"bytes 0-0/{item['size']}", f"got {status} {headers.get('content-range')}")
    for item in by_tier.get("over_limit", []):
        status, _, body = http_request(port, "GET", f"/v1/watcher/media/{item['filename']}", viewer, timeout=60)
        code = json.loads(body).get("code") if body else None
        c.check(f"media over contract limit ({item['size']} bytes) -> 503 media_too_large", status == 503 and code == "media_too_large", f"got {status} {code}")
    for filename in manifest["missing_media"][:1]:
        status, _, _ = http_request(port, "GET", f"/v1/watcher/media/{filename}", viewer)
        c.check("media referenced but absent -> 404", status == 404, f"got {status}")
    status, _, _ = http_request(port, "GET", f"/v1/watcher/media/{by_tier['small'][0]['filename']}")
    c.check("media without token -> 401", status == 401, f"got {status}")

    leaks = logs_leak_tokens(state_dir, tokens)
    c.check("no token value in any log file", not leaks, ", ".join(leaks))
    print(f"\n{c.failures} failure(s)")
    return 0 if c.failures == 0 else 1


# --------------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="wac-100 local test backend (watcher + watcher gateway, synthetic data)")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"gateway port on 127.0.0.1 (default {DEFAULT_PORT})")
        p.add_argument("--state-dir", help="override the state dir (default: $TMPDIR/wac-local-backend-<port>)")
        return p

    up = common(sub.add_parser("up", help="seed data and start watcher + gateway"))
    up.add_argument("--role", default=DEFAULT_ROLE, help="control-plane app role hosting the gateway (operator-query today; watcher-gateway later)")
    up.add_argument("--app", default="read_api:app", help="ASGI app to serve from services/control-plane/api")
    up.add_argument("--python", help="python with uvicorn/fastapi/httpx/psycopg2 (default: autodetect repo venvs)")
    up.add_argument("--node", help="node binary (default: node on PATH)")
    up.add_argument("--node-modules", help="watcher node_modules dir (default: worktree, then main checkout)")
    up.add_argument("--seed", type=int, default=seed.SeedSpec.seed)
    up.add_argument("--keep-on-failure", action="store_true")
    common(sub.add_parser("down", help="stop our processes and delete the state dir"))
    common(sub.add_parser("status", help="show processes and gateway health"))
    common(sub.add_parser("verify", help="smoke-check auth, pagination and media through the gateway"))
    token = common(sub.add_parser("token", help="show a reader token fingerprint (or value with --reveal)"))
    token.add_argument("--role", choices=sorted(READER_TOKENS), default="viewer")
    token.add_argument("--reveal", action="store_true", help="print the token value to this terminal")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = {"up": cmd_up, "down": cmd_down, "status": cmd_status, "verify": cmd_verify, "token": cmd_token}[args.command]
    try:
        return handler(args)
    except BackendError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
