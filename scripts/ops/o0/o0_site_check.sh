#!/usr/bin/env bash
# O-0 jp-24 site check: READ-ONLY, self-contained, writes nothing on the host. DRAFT.
#
# Every run needs the user's authorization (o0-authorization-list.md O0-A01, exactly).
# Default mode prints the commands only.
#
#   Plan (local, no connection):
#     bash scripts/ops/o0/o0_site_check.sh
#   Execute (after authorization), output captured LOCALLY only:
#     ssh -p 53222 root@100.89.58.40 'bash -s -- --execute --auth-id O0-A01 --expect-sync-sha256 <sha>' \
#       < scripts/ops/o0/o0_site_check.sh > o0-site-check-$(date -u +%Y%m%dT%H%M%SZ).txt
#
# Output hygiene is deny-by-default:
# * env files: KEY NAMES only;
# * Caddy: the full configuration never leaves the host. S-03/S-04 print an ALLOWLIST
#   skeleton (matchers, handler order, rewrite, dials, header NAMES, {placeholder} values;
#   every other string becomes <literal len=N>) and a host-side yes/no comparison of the
#   file-adapted and the running config; the Caddyfile outline (S-02) prints directives
#   with classified arguments only;
# * compose: environment values hidden except a non-secret allowlist;
# * a final line filter (bearer, bcrypt, long opaque strings, query values, secret-looking
#   KEY=VALUE) applies to the remaining free text.
# Every command that could read stdin is given </dev/null, because this script itself
# arrives on stdin. tests/site_check_leak_test.sh runs it against sentinel secrets.
set -eo pipefail

MODE="plan"
AUTH_ID=""
EXPECT_SYNC_SHA256=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --execute) MODE="execute" ;;
    --plan|--dry-run) MODE="plan" ;;
    --auth-id) AUTH_ID="${2:-}"; shift ;;
    --expect-sync-sha256) EXPECT_SYNC_SHA256="${2:-}"; shift ;;   # sha256 of bundle/tools/sync_operator_risk_db.py
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

CADDYFILE="${CADDYFILE:-/etc/caddy/Caddyfile}"
CADDY_ENV="${CADDY_ENV:-/etc/caddy/v3.env}"
WATCHER_ROOT="${WATCHER_ROOT:-/srv/trader}"
WATCHER_CONTAINER="${WATCHER_CONTAINER:-trader-watcher-1}"
WATCHER_DB="${WATCHER_DB:-/var/lib/docker/volumes/trader_signal-data/_data/watcher-trading.db}"
REPLICA_DB="${REPLICA_DB:-/srv/trader-v3/state/operator-query-risk/trading-risk.db}"
SYNC_SCRIPT="${SYNC_SCRIPT:-/srv/trader-v3/scripts/sync_operator_risk_db.py}"
OQ_ENV_FILE="${OQ_ENV_FILE:-/srv/trader-v3/secrets/control-plane/operator-query.env}"
CP_UNITS="trader-v3-controlplane-operator-query trader-v3-controlplane-node-control trader-v3-controlplane-event-ingest"
VENV_PY="${VENV_PY:-/srv/trader-v3/.venv-cp/bin/python}"
JP24_MARKER="${JP24_MARKER:-/srv/trader-v3}"   # overridable only for the local leak-test harness
NODE_APP_ROOT="${NODE_APP_ROOT:-/app}"          # node image code root (services/nautilus-node)
HB_SAMPLES="${HB_SAMPLES:-6}"                   # S-00 observed heartbeat age: samples x gap seconds
HB_SAMPLE_GAP="${HB_SAMPLE_GAP:-2}"

if [ "$MODE" = "execute" ]; then
  [ "$AUTH_ID" = "O0-A01" ] || { echo "ABORT: the site check needs exactly --auth-id O0-A01" >&2; exit 2; }
  [ "$(id -u)" = "0" ] || { echo "ABORT: run as root on jp-24" >&2; exit 2; }
  [ -d "$JP24_MARKER" ] || { echo "ABORT: $JP24_MARKER missing (not jp-24)" >&2; exit 2; }
  echo "O0_SITE_CHECK auth=$AUTH_ID host=$(hostname) at=$(date -u +%Y-%m-%dT%H:%M:%SZ) mode=read-only"
fi

# Shared Python helpers (kept identical in behaviour to o0_tool.py redact_line and
# caddy_skeleton; the leak test compares both on the same fixtures).
# read -d '' (not $(cat <<...)): bash 3.2 mis-parses quotes inside a heredoc in $(...)
IFS= read -r -d '' O0_PYLIB <<'PY' || [ -n "$O0_PYLIB" ]
import json, os, re, subprocess, sys
SECRET_KEY_RE = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|HASH|SESSION|API_KEY|APIKEY|PRIVATE|AUTH_JSON|CREDENTIAL)", re.I)
REDACTIONS = (
    (re.compile(r"\$2[abxy]?\$\d{2}\$[./A-Za-z0-9]{53}"), "<bcrypt-hash>"),
    (re.compile(r"(?i)(bearer\s+)(?!\{)[^\s\"'}]+"), r"\1<redacted>"),
    (re.compile(r"(?i)((?:basic_?auth|basicauth)\s+\S+\s+)(?!\{)\S+"), r"\1<redacted>"),
    (re.compile(r"([?&][A-Za-z0-9_.-]+=)(?!\{)[^&\s\"'#]+"), r"\1<redacted>"),
)
LONG_OPAQUE = re.compile(r"(?<![A-Za-z0-9_./-])(?=[A-Za-z0-9_+=-]*[0-9])(?=[A-Za-z0-9_+=-]*[A-Za-z])[A-Za-z0-9_+=-]{28,}(?![A-Za-z0-9_./-])")
KV = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*[=:]\s*)(.*)$")
PATH_CHUNK_SPLIT = re.compile(r"(/+)")
PATH_SEG_SPLIT = re.compile(r"([/^$.*+?()\[\]{}|\\]+)")
PATH_WORD = re.compile(r"[^\s\"']*/[^\s\"']*")
CHUNK_PLACEHOLDER = re.compile(r"\{[A-Za-z0-9_.$:-]+\}")
CHUNK_REGEX_SYNTAX = re.compile(r"\[[^\]]*\][*+?]?|\(\?[A-Za-z:]*|[\\^$()|*?{}\[\]]")
CHUNK_SEPARATORS = re.compile(r"[.\-_:~%&,;!@]")
PLACEHOLDER_RE = re.compile(r"^(Bearer |Basic )?\{[A-Za-z0-9_.$:-]+\}$")
HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")
SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_@.:*/+-]{1,64}$")
SKELETON_STRING_KEYS = {"handler", "dial", "listen", "host", "path", "method", "strip_path_prefix", "strip_path_suffix",
                        "group", "protocol", "name", "pattern", "@id", "root", "flush_interval", "uri"}
SKELETON_PATH_KEYS = {"path", "pattern", "uri", "root", "strip_path_prefix", "strip_path_suffix"}

def _token_segment(seg):
    if len(seg) >= 12 and re.search(r"[A-Za-z]", seg) and re.search(r"[0-9]", seg):
        return True
    return re.fullmatch(r"[0-9A-Fa-f]{16,}", seg) is not None

def _chunk_is_token(chunk):
    bare = CHUNK_REGEX_SYNTAX.sub("", CHUNK_PLACEHOLDER.sub("", chunk))
    return _token_segment(bare) or _token_segment(CHUNK_SEPARATORS.sub("", bare).replace("+", "").replace("=", ""))

def _redact_pieces(chunk):
    parts = PATH_SEG_SPLIT.split(chunk)
    return "".join(f"<seg len={len(p)}>" if i % 2 == 0 and p and _token_segment(p) else p for i, p in enumerate(parts))

def redact_path(value):
    out = []
    for i, chunk in enumerate(PATH_CHUNK_SPLIT.split(value)):
        if i % 2 == 1 or not chunk:
            out.append(chunk)
        elif _chunk_is_token(chunk):
            out.append(f"<seg len={len(chunk)}>")
        else:
            out.append(_redact_pieces(chunk))
    return "".join(out)

def redact_line(line):
    if line.lstrip().startswith("Environment="):
        names = [a.split("=", 1)[0].strip('"') for a in line.split("=", 1)[1].split()]
        return "Environment=<names:" + ",".join(names) + ">"
    m = KV.match(line)
    if m and SECRET_KEY_RE.search(m.group(2)) and m.group(4).strip() and not m.group(4).strip().startswith("{"):
        line = f"{m.group(1)}{m.group(2)}{m.group(3)}<redacted len={len(m.group(4).strip())}>"
    for pattern, repl in REDACTIONS:
        line = pattern.sub(repl, line)
    line = LONG_OPAQUE.sub("<redacted-opaque>", line)
    return PATH_WORD.sub(lambda m: redact_path(m.group(0)), line)

def _literal(value):
    return f"<literal len={len(str(value))}>"

def caddy_skeleton(node, ctx=()):
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
            out[k] = caddy_skeleton(value, ctx + (extra if key != "handler" else ()) + (key.lower(),))
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

def load_env(path):
    env = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        env[k.strip()] = v
    return env

def adapt_file(env_file, caddyfile):
    env = dict(os.environ)
    env.update(load_env(env_file))
    r = subprocess.run(["caddy", "adapt", "--adapter", "caddyfile", "--config", caddyfile], env=env,
                       stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"ADAPT_FAILED rc={r.returncode} stderr_lines={len(r.stderr.splitlines())}")
        sys.exit(1)
    return json.loads(r.stdout)

def running_config():
    r = subprocess.run(["curl", "-s", "-m", "5", "http://127.0.0.1:2019/config/"], stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        print(f"ADMIN_API_UNREADABLE rc={r.returncode}")
        sys.exit(1)
    return json.loads(r.stdout)

def diff_pointers(a, b, path=""):
    if type(a) is not type(b):
        yield path or "/"
    elif isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            safe = k if (SAFE_KEY_RE.match(k) and LONG_OPAQUE.search(k) is None and redact_line(k) == k) else f"<key len={len(k)}>"
            if k not in a or k not in b:
                yield f"{path}/{safe}"
            else:
                yield from diff_pointers(a[k], b[k], f"{path}/{safe}")
    elif isinstance(a, list):
        if len(a) != len(b):
            yield f"{path} (length {len(a)} vs {len(b)})"
        for i, (x, y) in enumerate(zip(a, b)):
            yield from diff_pointers(x, y, f"{path}/{i}")
    elif a != b:
        yield path or "/"

def print_skeleton(label, cfg):
    print(f"-----BEGIN O0 {label} SKELETON JSON-----")
    print(json.dumps(caddy_skeleton(cfg), indent=1, sort_keys=True))
    print(f"-----END O0 {label} SKELETON JSON-----")

DIRECTIVES = {"import", "handle", "handle_path", "handle_errors", "route", "basic_auth", "basicauth", "reverse_proxy", "uri",
              "header_up", "header_down", "request_header", "header", "path", "path_regexp", "method", "redir", "respond",
              "file_server", "root", "rewrite", "try_files", "encode", "host", "not", "strip_prefix", "strip_suffix",
              "log", "tls", "php_fastcgi", "abort", "error", "vars", "map", "forward_auth", "expression", "remote_ip", "client_ip",
              # wac-060, F-13 (2): global order options and every directive that runs before handle must be visible
              "order", "tracing", "fs", "log_append", "skip_log", "log_skip", "log_name", "request_body", "push", "intercept",
              "templates", "invoke"}
HEADER_DIRECTIVES = {"header_up", "header_down", "request_header", "header"}
SAFE_WORDS = {"{", "}", "*", "strip_prefix", "strip_suffix", "replace", "flush_interval", "bcrypt", "GET", "HEAD", "POST", "PUT",
              "DELETE", "PATCH", "OPTIONS", "-1", "not", "off", "on"}
SAFE_ARG = re.compile(r"^(/[A-Za-z0-9_./*{}-]*|\^/[^\s]*|@[A-Za-z0-9_-]+|\{[A-Za-z0-9_.$:-]+\}|[A-Za-z0-9.-]+:[0-9]+|:[0-9]+|[0-9]+|[A-Za-z0-9-]+\.[A-Za-z0-9.-]+,?)$")

def classify(tok):
    t = tok.strip('"')
    if t in SAFE_WORDS or SAFE_ARG.match(t):
        return redact_path(tok) if "/" in t else tok
    return _literal(t)

def outline_tokens(toks):
    head = toks[0]
    out = [head if head in DIRECTIVES or re.match(r"^@[A-Za-z0-9_-]+$", head) else _literal(head)]
    args = toks[1:]
    if head.startswith("@") and args and args[0] in DIRECTIVES:
        return out + outline_tokens(args)
    if head in HEADER_DIRECTIVES:
        if args and args[0].startswith("@"):
            out.append(classify(args[0])); args = args[1:]
        if args:
            name = args[0]
            out.append(name if HEADER_NAME_RE.match(name.lstrip("+-><")) and redact_line(name) == name else _literal(name))
            out += [a if PLACEHOLDER_RE.match(a.strip('"')) or a in ("{", "}") else _literal(a.strip('"')) for a in args[1:]]
    elif head == "order":
        # directive names and before/after/first/last only: lower-case LETTERS with single underscores (no digits,
        # so a token-like word cannot pass; the leak test pins it); anything else is classified
        out += [a if re.fullmatch(r"[a-z]{1,24}(?:_[a-z]{1,24}){0,3}", a) else classify(a) for a in args]
    elif head == "respond":
        out += [a if re.match(r"^([0-9]{3}|@[A-Za-z0-9_-]+|\{|\})$", a) else _literal(a.strip('"')) for a in args]
    else:
        out += [a if a in DIRECTIVES else classify(a) for a in args]
    return out

def caddyfile_outline(path):
    for n, raw in enumerate(open(path, encoding="utf-8"), 1):
        line = raw.rstrip("\n")
        body = line.strip()
        if not body or body.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        toks = re.findall(r'"[^"]*"|\S+', body)
        head = toks[0]
        if indent == 0 and body.endswith("{") and head not in DIRECTIVES and not head.startswith("@"):
            out = [classify(t) for t in toks]
        elif head in DIRECTIVES or head.startswith("@"):
            out = outline_tokens(toks)
        else:
            continue
        # the line filter runs per token (a whole-line filter would re-split the markers)
        out = [t if t.startswith("<literal len=") else redact_line(t) for t in out]
        print(f"{n:4d}: {'  ' * (indent // 4 if indent >= 4 else indent)}{' '.join(out)}")

def compose_watcher_block(path):
    lines = open(path, encoding="utf-8").read().splitlines()
    allow = {"TRADER_TRADING_DB_PATH", "WATCHER_TRADING_DB", "TRADING_DB_PATH", "WATCHER_HOST", "WATCHER_MEDIA_DIR",
             "HERMES_TRADER_CRON_ENABLED", "SIGNAL_IMPORTER_CWD", "TELEGRAM_PROXY_ENABLED", "TELEGRAM_USE_WSS", "PRICE_MONITOR_ENABLED"}
    inside = in_env = False
    env_indent = 0
    for raw in lines:
        if re.match(r"^  watcher:\s*$", raw):
            inside = True
        elif inside and re.match(r"^  [A-Za-z0-9_-]+:\s*$", raw):
            break
        if not inside:
            continue
        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip())
        if in_env and stripped and indent <= env_indent:
            in_env = False
        if re.match(r"^\s+environment:\s*$", raw):
            in_env, env_indent = True, indent
            print(raw)
            continue
        if in_env:
            if not stripped or stripped.startswith("#"):
                continue
            m = re.match(r"^(\s*-?\s*)([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*(.*)$", raw)
            if m:
                key, val = m.group(2), m.group(3).strip()
                shown = val if key in allow and redact_line(val) == val else f"<value hidden len={len(val)}>"
                print(f"{m.group(1)}{key}: {shown}")
            else:
                print(f"{' ' * indent}<unparsed env line hidden>")
            continue
        print(redact_line(raw))
PY
export O0_PYLIB

# S-00 heartbeat parameters, run INSIDE each node container with its own python: prints only
# numbers (never the config, never a token); any error prints its class name only.
IFS= read -r -d '' NODE_HB_PY <<'PY' || [ -n "$NODE_HB_PY" ]
import json, re, sys
def num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else "<non-numeric>"
try:
    cp = json.load(open(sys.argv[1])).get("control_plane") or {}
    print("heartbeat_timeout_seconds=%s" % num(cp.get("heartbeat_timeout_seconds")), end=" ")
except Exception as exc:
    print("heartbeat_timeout_seconds=<unreadable:%s>" % type(exc).__name__, end=" ")
try:
    m = re.search(r"^DEFAULT_HEARTBEAT_INTERVAL_SECONDS\s*=\s*([0-9.]+)\s*$", open(sys.argv[2] + "/runtime/control_plane_session.py").read(), re.M)
    print("default_heartbeat_interval_seconds=%s" % (m.group(1) if m else "<not-found>"), end=" ")
except Exception as exc:
    print("default_heartbeat_interval_seconds=<unreadable:%s>" % type(exc).__name__, end=" ")
try:
    print("node_py_overrides_interval=%s" % ("heartbeat_interval_seconds" in open(sys.argv[2] + "/app/node.py").read()))
except Exception as exc:
    print("node_py_overrides_interval=<unreadable:%s>" % type(exc).__name__)
PY
export NODE_HB_PY

section() { printf '\n=== %s\n' "$*"; }
run() {
  # run "S-xx description" 'shell text'
  local id="$1" text="$2"
  if [ "$MODE" = "plan" ]; then
    printf '%s\n    $ %s\n' "$id" "$text"
    return 0
  fi
  section "$id"
  printf '$ %s\n' "$text"
  set +e
  bash -o pipefail -c "$text" </dev/null
  local rc=$?
  set -e
  printf -- '--- exit=%s\n' "$rc"
}

PYRUN='python3 -c "import os,sys; exec(os.environ[\"O0_PYLIB\"]); '
REDACT="${PYRUN}[print(redact_line(l.rstrip(chr(10)))) for l in sys.stdin]\""
# Env-file key names only. The FILE is an argument of sed (a trailing "< file" would bind
# to sort and print values).
keys_of() { printf "sed -E -e '/^[[:space:]]*(#|\$)/d' -e 's/^[[:space:]]*export[[:space:]]+//' -e 's/=.*//' -- %s | sort" "$1"; }

run "S-00 fleet state before any O-0 action (docs/agent-operations.md §0); node ids = O0_FLEET_NODES for every fleet guard" \
  "docker exec trader-v3-postgres psql -U postgres -d trader -Atc \"SELECT node_id||' '||coalesce(status::text,'NULL')||' '||coalesce(release_id::varchar(12),'NULL')||' hb_age='||coalesce(round(extract(epoch from now()-last_seen_at)::numeric,1)::text,'NULL') FROM node_heartbeats ORDER BY node_id\"; for p in 8081 8082 8083 8084; do printf 'ready:%s %s\n' \$p \"\$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:\$p/ready || true)\"; done
   echo '--- node heartbeat parameters for the fleet guard (O0_NODE_HB_INTERVAL_S / O0_NODE_HB_TIMEOUT_S; review wac-032-r2 🟡-7)'
   cs=\$(docker ps --format '{{.Names}}' | grep -E '^trader-v3-node-' | sort || true); echo \"node_containers=\$(printf '%s' \"\$cs\" | grep -c . || true)\"
   for c in \$cs; do
     cfg=\$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' \$c | sed -n 's/^NODE_CONFIG_PATH=//p')
     printf '%s NODE_CONFIG_PATH=%s ' \$c \"\${cfg:-<unset>}\"; docker exec \$c python3 -c \"\$NODE_HB_PY\" \"\$cfg\" '$NODE_APP_ROOT' || echo 'NODE_HB_PARAMS_UNREADABLE'
   done
   echo '--- observed heartbeat age ($HB_SAMPLES samples, ${HB_SAMPLE_GAP}s apart): the max approximates the real interval'
   i=0; while [ \$i -lt $HB_SAMPLES ]; do docker exec trader-v3-postgres psql -U postgres -d trader -Atc \"SELECT node_id||' '||coalesce(status::text,'NULL')||' '||coalesce(release_id::varchar(12),'NULL')||' hb_age='||coalesce(round(extract(epoch from now()-last_seen_at)::numeric,1)::text,'NULL') FROM node_heartbeats ORDER BY node_id\"; i=\$((i + 1)); [ \$i -ge $HB_SAMPLES ] || sleep $HB_SAMPLE_GAP; done \\
     | awk '{n[\$1]++; split(\$4, a, \"=\"); if (a[2] !~ /^-?[0-9]+([.][0-9]+)?\$/) {bad[\$1]++; why[\$1] = (a[2] ~ /^[A-Za-z]+\$/) ? a[2] : \"non-numeric\"; next} v = a[2] + 0; if (!(\$1 in m) || v > m[\$1]) m[\$1] = v} END {for (k in n) if (k in bad) printf \"observed_max_hb_age %s <uncomparable:%s> samples=%d non_numeric=%d\\n\", k, why[k], n[k], bad[k]; else printf \"observed_max_hb_age %s %.1f samples=%d\\n\", k, m[k], n[k]}' | sort"

run "S-01 caddy version, unit, EnvironmentFile key names" \
  "caddy version; systemctl show caddy -p ActiveState,SubState,ExecMainStartTimestamp,NRestarts,EnvironmentFiles; systemctl cat caddy | grep -E '^(ExecStart|ExecReload|EnvironmentFile)' | $REDACT; echo '--- keys in $CADDY_ENV'; $(keys_of "'$CADDY_ENV'"); stat -c '%a %U:%G %s %y %n' '$CADDY_ENV' '$CADDYFILE'"

run "S-02 Caddyfile imports and directive outline (arguments classified; literals shown as <literal len=N>); global order options and pre-handle directives shown for the F-13 (2) record" \
  "sha256sum '$CADDYFILE' | cut -c1-16; ${PYRUN}caddyfile_outline(sys.argv[1])\" '$CADDYFILE'"

run "S-03 Caddyfile adapted with v3.env loaded WITHOUT shell expansion -> ALLOWLIST skeleton only (the full JSON never leaves the host)" \
  "${PYRUN}print_skeleton(\\\"ADAPTED FILE\\\", adapt_file(sys.argv[1], sys.argv[2]))\" '$CADDY_ENV' '$CADDYFILE'"

run "S-04 running config (admin API) vs file-adapted config compared ON THE HOST; verdict + differing JSON paths (no values) + running skeleton" \
  "${PYRUN}f=adapt_file(sys.argv[1], sys.argv[2]); r=running_config(); d=list(diff_pointers(f, r)); print(\\\"RUNNING_EQUALS_FILE=\\\" + (\\\"yes\\\" if not d else \\\"no\\\")); [print(\\\"DIFF_PATH\\\", p) for p in d[:40]]; print_skeleton(\\\"RUNNING\\\", r)\" '$CADDY_ENV' '$CADDYFILE'"

run "S-05 watcher container: image, user (owner of the DB files, restore-db), labels, ports, restart, health, env NAMES, DB/host env values" \
  "docker inspect -f 'image={{.Image}} config_image={{.Config.Image}} user={{if .Config.User}}{{.Config.User}}{{else}}<image-default-root>{{end}} started={{.State.StartedAt}} restarts={{.RestartCount}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' $WATCHER_CONTAINER; docker inspect -f '{{json .HostConfig.PortBindings}}' $WATCHER_CONTAINER; docker inspect -f '{{json .Config.Labels}}' $WATCHER_CONTAINER | python3 -c 'import json,sys; d=json.load(sys.stdin) or {}; [print(k,\"=\",v) for k,v in sorted(d.items()) if k.startswith((\"org.trader\",\"com.docker.compose\"))]'; echo '--- env names'; docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' $WATCHER_CONTAINER | sed 's/=.*//' | sort; echo '--- non-secret env values'; docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' $WATCHER_CONTAINER | grep -E '^(TRADER_TRADING_DB_PATH|WATCHER_TRADING_DB|TRADING_DB_PATH|WATCHER_MEDIA_DIR|WATCHER_HOST|PRICE_MONITOR_ENABLED|HERMES_TRADER_CRON_ENABLED)='; docker inspect -f '{{range .Mounts}}{{.Type}} {{.Source}} -> {{.Destination}} rw={{.RW}}{{println}}{{end}}' $WATCHER_CONTAINER"

run "S-06 listening sockets: watcher (only 127.0.0.1:9090), operator-query, Caddy admin, node channel (confirm the address used by the stage C probe, expected 172.30.1.1:8080); WGW-1.0.4 PC-6 (ii): every listener on 8184-8189 (8186 must be free before stage O, loopback-only after)" \
  "ss -ltnpH | grep -E ':(9090|9100|8181|8182|8183|8080|2019|443|80)[[:space:]]' | awk '{print \$4, \$6}' | sort -u; echo '--- node channel candidates (:8080)'; ss -ltnH | awk '{print \$4}' | grep -E ':8080\$' | sort -u; echo '--- 8184-8189 (PC-6 (ii))'; ss -ltnpH | awk '{print \$4, \$6}' | grep -E ':818[4-9] ' | sort -u; ss -ltnH | awk '{print \$4}' | grep -qE ':8186\$' && echo 'PORT 8186 IN USE' || echo 'PORT 8186 free'"

run "S-07 watcher source tree and compose on disk (sha256 for baseline compare; config.json must NOT be in the build context); compose env values hidden" \
  "cd '$WATCHER_ROOT/services/telegram-watcher' && find . -path ./node_modules -prune -o -type f \\( -name '*.js' -o -name '*.json' -o -name '*.html' -o -name '*.py' -o -name '*.md' -o -name '*.sh' \\) -print | sort | sed 's|^\\./||' | while read -r f; do sha256sum \"\$PWD/\$f\"; done; if test -e config.json; then echo 'WARNING config.json PRESENT in build context'; else echo 'config.json absent from build context'; fi; sha256sum '$WATCHER_ROOT/docker-compose.yml'; ${PYRUN}compose_watcher_block(sys.argv[1])\" '$WATCHER_ROOT/docker-compose.yml'; for e in '$WATCHER_ROOT'/.env*; do if [ -e \"\$e\" ]; then stat -c '%a %U:%G %n' \"\$e\"; fi; done"

run "S-08 watcher DB (live volume, opened read-only): files, tables, config row counts, revision presence" \
  "ls -l --time-style=+%FT%T '$WATCHER_DB'*; command -v sqlite3 || echo 'sqlite3 absent (use python3 backup API)'; python3 -c 'import sqlite3,sys
c=sqlite3.connect(\"file:\"+sys.argv[1]+\"?mode=ro\",uri=True)
t=[r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type=\x27table\x27 ORDER BY name\")]
print(\"tables:\",\" \".join(t))
for n in (\"account_configs\",\"channel_routing\",\"symbol_risk_configs\",\"config_revision\",\"config_audit\",\"price_alerts\"):
    print(n, c.execute(\"SELECT count(*) FROM \"+n).fetchone()[0] if n in t else \"ABSENT\")
cols=[r[1] for r in c.execute(\"PRAGMA table_info(account_configs)\")]
print(\"account_configs columns:\", \" \".join(x for x in cols if x not in (\"api_key\",\"api_secret\")), \"(+secret columns hidden)\")
print(\"account_configs has CHECK:\", \"CHECK\" in (c.execute(\"SELECT sql FROM sqlite_master WHERE name=\x27account_configs\x27\").fetchone()[0] or \"\").upper())' '$WATCHER_DB'"

run "S-09 replica and sync tooling; the host's sync script is executed (--check, read-only) ONLY if its sha256 equals the reviewed one" \
  "stat -c '%a %U:%G %s %y %n' '$REPLICA_DB'; systemctl list-unit-files 'trader-v3-operator-risk-db-sync*' --no-pager; systemctl list-timers 'trader-v3-operator-risk-db-sync*' --no-pager --all
   if [ ! -f '$SYNC_SCRIPT' ]; then echo 'SYNC_SCRIPT_ABSENT (P-17: the bundle carries tools/sync_operator_risk_db.py)'; exit 0; fi
   have=\$(sha256sum '$SYNC_SCRIPT' | cut -d' ' -f1); echo \"sync_script_sha256=\$have\"
   if [ -z '$EXPECT_SYNC_SHA256' ]; then echo 'SYNC_CHECK_PENDING: no --expect-sync-sha256 given; unknown code is not executed'; exit 0; fi
   if [ \"\$have\" != '$EXPECT_SYNC_SHA256' ]; then echo 'SYNC_CHECK_SKIPPED: host script differs from the reviewed sha256; unknown code is not executed'; exit 0; fi
   rc=0; python3 '$SYNC_SCRIPT' --check --source '$WATCHER_DB' --target '$REPLICA_DB' || rc=\$?; echo \"sync --check exit=\$rc (0 unchanged, 3 drift)\""

run "S-10 control-plane units (all three): state, worker count, working directory, env files, user, NeedDaemonReload + unit file paths (stage O gate treats NeedDaemonReload=yes as UNCOMPARABLE); node-control/event-ingest must NOT load operator-query.env" \
  "for u in $CP_UNITS; do echo \"--- \$u\"; systemctl show \$u -p ActiveState,SubState,MainPID,ExecMainStartTimestamp,NRestarts,WorkingDirectory,User,EnvironmentFiles,NeedDaemonReload,FragmentPath,DropInPaths; systemctl cat \$u | grep -E '^(ExecStart|Environment=|EnvironmentFile|WorkingDirectory)' | $REDACT; done
   for u in trader-v3-controlplane-node-control trader-v3-controlplane-event-ingest; do
     if systemctl show \$u -p EnvironmentFiles --value | grep -F -e '$OQ_ENV_FILE' -e 'operator-query.env' >/dev/null; then echo \"ENVFILE_ISOLATION VIOLATION \$u loads operator-query.env (would hold WATCHER_GATEWAY/SNAPSHOT_TOKEN after O-2)\"; else echo \"ENVFILE_ISOLATION ok \$u\"; fi
   done"

run "S-11 operator-query env: key NAMES only, plus the non-secret DB alias paths and snapshot switch" \
  "for f in \$(systemctl show trader-v3-controlplane-operator-query -p EnvironmentFiles --value | sed -E 's/ \\(ignore_errors=[a-z]+\\)//g'); do f=\${f#-}; echo \"--- keys in \$f\"; sed -E -e '/^[[:space:]]*(#|\$)/d' -e 's/^[[:space:]]*export[[:space:]]+//' -e 's/=.*//' -- \"\$f\" | sort; grep -E '^(TRADER_TRADING_DB_PATH|WATCHER_TRADING_DB|TRADING_DB_PATH|WATCHER_CONFIG_SNAPSHOT_ENABLED|WATCHER_SNAPSHOT_URL|WATCHER_GATEWAY_URL)=' \"\$f\" || echo '(none of the non-secret keys set)'; done"

run "S-12 production venv: Python >= 3.11 and gateway dependencies (wac-016 🟡-8; also runs the offline image builder)" \
  "'$VENV_PY' -c 'import sys,httpx,fastapi,starlette,tomllib; print(\"python\",sys.version.split()[0],\"httpx\",httpx.__version__,\"fastapi\",fastapi.__version__,\"starlette\",starlette.__version__); assert sys.version_info>=(3,11), \"PYTHON_TOO_OLD\"' && '$VENV_PY' -m uvicorn --version"

run "S-13 control-plane code on disk: sha256 of api/*.py, api/generated/*.py, security/*.py (baseline compare locally)" \
  "wd=\$(systemctl show trader-v3-controlplane-operator-query -p WorkingDirectory --value); root=\$(dirname \"\$wd\"); echo \"working_directory=\$wd root=\$root\"; find \"\$root/api\" \"\$root/security\" -maxdepth 2 -name '*.py' -type f | sort | xargs sha256sum"

run "S-14 staging area and disk (staging must be /srv/trader-staging, /tmp is tmpfs)" \
  "ls -ld /srv/trader-staging; df -h /srv /srv/trader-staging /tmp | sed 1d; du -sh /srv/trader-v3/backups 2>/dev/null || echo 'no /srv/trader-v3/backups'"

run "S-15 other writers of the watcher config tables (D1): db_manager.py callers" \
  "grep -rls 'db_manager' /etc/cron* /var/spool/cron /etc/systemd/system 2>/dev/null || echo 'no cron/systemd reference'; ps -eo pid,etime,cmd | grep -E 'db_manager\\.py' | grep -v grep || echo 'no running db_manager'"

run "S-16 hermes-feeder direct reader of the real DB (unchanged by O-0)" \
  "systemctl show trader-v3-hermes-feeder -p ActiveState,User,EnvironmentFiles; systemctl cat trader-v3-hermes-feeder | grep -E '^(ExecStart|Environment=|EnvironmentFile)' | $REDACT"

run "S-17 recent error baseline (1h): operator-query journal, watcher logs, caddy journal (counts only)" \
  "echo \"operator-query errors: \$(journalctl -u trader-v3-controlplane-operator-query --since -1h --no-pager | awk 'tolower(\$0) ~ /error|traceback/{c++} END{print c+0}')\"; echo \"watcher db failures: \$(docker logs --since 1h $WATCHER_CONTAINER 2>&1 | awk '/\\[db\\] Failed/{c++} END{print c+0}')\"; echo \"watcher exits: \$(docker logs --since 24h $WATCHER_CONTAINER 2>&1 | awk 'tolower(\$0) ~ /exitwatcher|exiting/{c++} END{print c+0}')\"; echo \"caddy errors: \$(journalctl -u caddy --since -1h --no-pager | awk '/\"level\":\"error\"/{c++} END{print c+0}')\""

run "S-18 host-local HTTP probes without credentials (watcher is still unauthenticated before W-0 ships)" \
  "for u in http://127.0.0.1:9090/api/status http://127.0.0.1:9090/healthz http://127.0.0.1:8183/v1/accounts http://127.0.0.1:8183/v1/watcher/status; do printf '%s ' \$u; curl -s -o /dev/null -w '%{http_code}\n' -m 5 \$u || echo 000; done"

if [ "$MODE" = "plan" ]; then
  printf '\nPLAN_ONLY: nothing was executed. Run on jp-24 only after the user authorizes O0-A01.\n'
else
  printf '\nO0_SITE_CHECK_DONE at=%s (read-only; no file written on the host)\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
fi
