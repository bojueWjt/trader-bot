#!/usr/bin/env bash
# O-0 jp-24 site check: READ-ONLY, self-contained, writes nothing on the host. DRAFT.
#
# Every run needs the user's authorization (o0-authorization-list.md O0-A01).
# Default mode prints the commands only.
#
#   Plan (local, no connection):
#     bash scripts/ops/o0/o0_site_check.sh
#   Execute (after authorization), output captured LOCALLY only:
#     ssh -p 53222 root@100.89.58.40 'bash -s -- --execute --auth-id O0-A01' \
#       < scripts/ops/o0/o0_site_check.sh > o0-site-check-$(date -u +%Y%m%dT%H%M%SZ).txt
#
# Output hygiene: env files are printed as KEY NAMES only; config dumps go
# through an inline redactor; the adapted Caddy JSON has basic-auth passwords
# and literal header values replaced (placeholders such as {env.X} are kept).
# Every command that could read stdin is given </dev/null, because this script
# itself arrives on stdin.
set -eo pipefail

MODE="plan"
AUTH_ID=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --execute) MODE="execute" ;;
    --plan|--dry-run) MODE="plan" ;;
    --auth-id) AUTH_ID="${2:-}"; shift ;;
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
CP_UNITS="trader-v3-controlplane-operator-query trader-v3-controlplane-node-control trader-v3-controlplane-event-ingest"
VENV_PY="${VENV_PY:-/srv/trader-v3/.venv-cp/bin/python}"
JP24_MARKER="${JP24_MARKER:-/srv/trader-v3}"   # overridable only for the local leak-test harness

if [ "$MODE" = "execute" ]; then
  [[ "$AUTH_ID" =~ ^O0-A01(-[A-Za-z0-9._:-]+)?$ ]] || { echo "ABORT: --auth-id O0-A01[-suffix] required" >&2; exit 2; }
  [ "$(id -u)" = "0" ] || { echo "ABORT: run as root on jp-24" >&2; exit 2; }
  [ -d "$JP24_MARKER" ] || { echo "ABORT: $JP24_MARKER missing (not jp-24)" >&2; exit 2; }
  echo "O0_SITE_CHECK auth=$AUTH_ID host=$(hostname) at=$(date -u +%Y-%m-%dT%H:%M:%SZ) mode=read-only"
fi

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

# Inline redactor (same rules as o0_tool.py redact): masks secret KEY=VALUE values,
# bearer values, bcrypt hashes and long opaque tokens.
REDACT='python3 -c "
import re,sys
S=re.compile(r\"(TOKEN|SECRET|PASSWORD|PASSWD|HASH|SESSION|API_KEY|APIKEY|PRIVATE|AUTH_JSON|CREDENTIAL)\",re.I)
KV=re.compile(r\"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*[=:]\s*)(.*)$\")
for l in sys.stdin:
    l=l.rstrip(chr(10))
    if l.lstrip().startswith(\"Environment=\"):
        print(\"Environment=<names:\"+\",\".join(a.split(chr(61),1)[0].strip(chr(34)) for a in l.split(chr(61),1)[1].split())+\">\"); continue
    m=KV.match(l)
    if m and S.search(m.group(2)) and m.group(4).strip() and not m.group(4).strip().startswith(chr(123)):
        l=m.group(1)+m.group(2)+m.group(3)+\"<redacted>\"
    l=re.sub(r\"\\\$2[abxy]?\\\$\d{2}\\\$[./A-Za-z0-9]{53}\",\"<bcrypt-hash>\",l)
    l=re.sub(r\"(?i)(bearer\s+)(?!\{)[^\s\\\"}]+\",r\"\1<redacted>\",l)
    l=re.sub(r\"(?<![A-Za-z0-9_./-])(?=[A-Za-z0-9_+=-]*[0-9])(?=[A-Za-z0-9_+=-]*[A-Za-z])[A-Za-z0-9_+=-]{28,}(?![A-Za-z0-9_./-])\",\"<redacted-opaque>\",l)
    print(l)
"'
# Env-file key names only. The FILE is an argument of sed (a trailing "< file" would bind
# to sort and print values).
keys_of() { printf "sed -E -e '/^[[:space:]]*(#|\$)/d' -e 's/^[[:space:]]*export[[:space:]]+//' -e 's/=.*//' -- %s | sort" "$1"; }

run "S-00 fleet state before any O-0 action (docs/agent-operations.md §0)" \
  "docker exec trader-v3-postgres psql -U postgres -d trader -Atc \"SELECT node_id||' '||status||' '||release_id::varchar(12)||' hb_age='||round(extract(epoch from now()-last_seen_at),1) FROM node_heartbeats ORDER BY node_id\"; for p in 8081 8082 8083 8084; do printf '%s:' \$p; curl -s -o /dev/null -w '%{http_code} ' -m 3 http://127.0.0.1:\$p/ready; done; echo"

run "S-01 caddy version, unit, EnvironmentFile key names" \
  "caddy version; systemctl show caddy -p ActiveState,SubState,ExecMainStartTimestamp,NRestarts,EnvironmentFiles; systemctl cat caddy | grep -E '^(ExecStart|ExecReload|EnvironmentFile)'; echo '--- keys in $CADDY_ENV'; $(keys_of "'$CADDY_ENV'"); stat -c '%a %U:%G %s %y %n' '$CADDY_ENV' '$CADDYFILE'"

run "S-02 Caddyfile imports and directive outline (redacted; order as written)" \
  "sha256sum '$CADDYFILE'; grep -nE '^[[:space:]]*import[[:space:]]' '$CADDYFILE' || echo 'no import lines'; grep -nE '(^[^#[:space:]].*\{[[:space:]]*\$|^[[:space:]]*(import|handle|handle_path|route|basic_?auth|reverse_proxy|uri|header_up|header_down|request_header|header|@[A-Za-z0-9_]+|path|path_regexp|method|redir|respond|file_server|root)\b)' '$CADDYFILE' | $REDACT"

run "S-03 adapted Caddy JSON of the file on disk (env loaded without shell expansion; redacted)" \
  "echo '-----BEGIN O0 ADAPTED FILE JSON-----'; python3 -c 'import os,sys
env=dict(os.environ)
for line in open(sys.argv[1],encoding=\"utf-8\"):
    line=line.strip()
    if not line or line.startswith(\"#\") or \"=\" not in line: continue
    if line.startswith(\"export \"): line=line[7:]
    k,v=line.split(\"=\",1); v=v.strip()
    if len(v)>=2 and v[0]==v[-1] and v[0] in \"\\x22\\x27\": v=v[1:-1]
    env[k.strip()]=v
os.execvpe(\"caddy\",[\"caddy\",\"adapt\",\"--adapter\",\"caddyfile\",\"--config\",sys.argv[2]],env)' '$CADDY_ENV' '$CADDYFILE' 2>/dev/null | python3 -c 'import json,re,sys
def r(n):
    if isinstance(n,dict):
        o={}
        for k,v in n.items():
            if k==\"password\" and isinstance(v,str): o[k]=\"<redacted>\"
            elif k in (\"set\",\"add\") and isinstance(v,dict): o[k]={a:[x if isinstance(x,str) and re.fullmatch(r\"(Bearer )?\{[A-Za-z0-9_.\$]+\}\",x) else \"<literal len=%d>\"%len(str(x)) for x in (b or [])] for a,b in v.items()}
            else: o[k]=r(v)
        return o
    if isinstance(n,list): return [r(x) for x in n]
    return n
json.dump(r(json.load(sys.stdin)),sys.stdout,indent=1,sort_keys=True)'; echo; echo '-----END O0 ADAPTED FILE JSON-----'"

run "S-04 running Caddy config from the admin API (redacted) — must equal S-03" \
  "echo '-----BEGIN O0 RUNNING JSON-----'; curl -s -m 5 http://127.0.0.1:2019/config/ | python3 -c 'import json,re,sys
def r(n):
    if isinstance(n,dict):
        o={}
        for k,v in n.items():
            if k==\"password\" and isinstance(v,str): o[k]=\"<redacted>\"
            elif k in (\"set\",\"add\") and isinstance(v,dict): o[k]={a:[x if isinstance(x,str) and re.fullmatch(r\"(Bearer )?\{[A-Za-z0-9_.\$]+\}\",x) else \"<literal len=%d>\"%len(str(x)) for x in (b or [])] for a,b in v.items()}
            else: o[k]=r(v)
        return o
    if isinstance(n,list): return [r(x) for x in n]
    return n
json.dump(r(json.load(sys.stdin)),sys.stdout,indent=1,sort_keys=True)'; echo; echo '-----END O0 RUNNING JSON-----'"

run "S-05 watcher container: image, labels, ports, restart, health, env NAMES, DB/host env values" \
  "docker inspect -f 'image={{.Image}} config_image={{.Config.Image}} started={{.State.StartedAt}} restarts={{.RestartCount}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' $WATCHER_CONTAINER; docker inspect -f '{{json .HostConfig.PortBindings}}' $WATCHER_CONTAINER; docker inspect -f '{{json .Config.Labels}}' $WATCHER_CONTAINER | python3 -c 'import json,sys; d=json.load(sys.stdin) or {}; [print(k,\"=\",v) for k,v in sorted(d.items()) if k.startswith((\"org.trader\",\"com.docker.compose\"))]'; echo '--- env names'; docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' $WATCHER_CONTAINER | sed 's/=.*//' | sort; echo '--- non-secret env values'; docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' $WATCHER_CONTAINER | grep -E '^(TRADER_TRADING_DB_PATH|WATCHER_TRADING_DB|TRADING_DB_PATH|WATCHER_MEDIA_DIR|WATCHER_HOST|PRICE_MONITOR_ENABLED|HERMES_TRADER_CRON_ENABLED)='; docker inspect -f '{{range .Mounts}}{{.Type}} {{.Source}} -> {{.Destination}} rw={{.RW}}{{println}}{{end}}' $WATCHER_CONTAINER"

run "S-06 listening sockets for watcher, operator-query and Caddy admin (only 127.0.0.1:9090 may expose the watcher)" \
  "ss -ltnpH | grep -E ':(9090|9100|8181|8182|8183|8080|2019|443|80)[[:space:]]' | awk '{print \$4, \$6}' | sort -u"

run "S-07 watcher source tree and compose on disk (sha256 for baseline compare; config.json must NOT be in the build context)" \
  "cd '$WATCHER_ROOT/services/telegram-watcher' && find . -path ./node_modules -prune -o -type f \\( -name '*.js' -o -name '*.json' -o -name '*.html' -o -name '*.py' -o -name '*.md' -o -name '*.sh' \\) -print | sort | sed 's|^\\./||' | while read -r f; do sha256sum \"\$PWD/\$f\"; done; test -e config.json && echo 'WARNING config.json PRESENT in build context' || echo 'config.json absent from build context'; sha256sum '$WATCHER_ROOT/docker-compose.yml'; awk '/^  watcher:/{f=1} f&&/^  [A-Za-z0-9_-]+:/&&!/^  watcher:/{f=0} f' '$WATCHER_ROOT/docker-compose.yml' | $REDACT; for e in '$WATCHER_ROOT'/.env*; do [ -e \"\$e\" ] && stat -c '%a %U:%G %n' \"\$e\"; done; true"

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

run "S-09 replica and sync tooling (read-only compare; exit 3 = drift)" \
  "stat -c '%a %U:%G %s %y %n' '$REPLICA_DB'; ls -l /srv/trader-v3/scripts/sync_operator_risk_db.py 2>&1; systemctl list-unit-files 'trader-v3-operator-risk-db-sync*' --no-pager; systemctl list-timers 'trader-v3-operator-risk-db-sync*' --no-pager --all; if [ -f /srv/trader-v3/scripts/sync_operator_risk_db.py ]; then python3 /srv/trader-v3/scripts/sync_operator_risk_db.py --check --source '$WATCHER_DB' --target '$REPLICA_DB'; echo \"sync --check exit=\$?\"; fi"

run "S-10 control-plane units (all three), worker count, working directory, env files, user" \
  "for u in $CP_UNITS; do echo \"--- \$u\"; systemctl show \$u -p ActiveState,SubState,MainPID,ExecMainStartTimestamp,NRestarts,WorkingDirectory,User,EnvironmentFiles; systemctl cat \$u | grep -E '^(ExecStart|Environment=|EnvironmentFile|WorkingDirectory)' | $REDACT; done"

run "S-11 operator-query env: key NAMES only, plus the non-secret DB alias paths and snapshot switch" \
  "for f in \$(systemctl show trader-v3-controlplane-operator-query -p EnvironmentFiles --value | sed -E 's/ \\(ignore_errors=[a-z]+\\)//g'); do f=\${f#-}; echo \"--- keys in \$f\"; sed -E -e '/^[[:space:]]*(#|\$)/d' -e 's/^[[:space:]]*export[[:space:]]+//' -e 's/=.*//' -- \"\$f\" | sort; grep -E '^(TRADER_TRADING_DB_PATH|WATCHER_TRADING_DB|TRADING_DB_PATH|WATCHER_CONFIG_SNAPSHOT_ENABLED|WATCHER_SNAPSHOT_URL|WATCHER_GATEWAY_URL)=' \"\$f\" || true; done"

run "S-12 production venv: Python >= 3.11 and gateway dependencies (wac-016 🟡-8)" \
  "'$VENV_PY' -c 'import sys,httpx,fastapi,starlette; print(\"python\",sys.version.split()[0],\"httpx\",httpx.__version__,\"fastapi\",fastapi.__version__,\"starlette\",starlette.__version__); assert sys.version_info>=(3,11), \"PYTHON_TOO_OLD\"' && '$VENV_PY' -m uvicorn --version"

run "S-13 control-plane code on disk: sha256 of api/*.py, api/generated/*.py, security/*.py (baseline compare locally)" \
  "wd=\$(systemctl show trader-v3-controlplane-operator-query -p WorkingDirectory --value); root=\$(dirname \"\$wd\"); echo \"working_directory=\$wd root=\$root\"; find \"\$root/api\" \"\$root/security\" -maxdepth 2 -name '*.py' -type f | sort | xargs sha256sum"

run "S-14 staging area and disk (staging must be /srv/trader-staging, /tmp is tmpfs)" \
  "ls -ld /srv/trader-staging; df -h /srv /srv/trader-staging /tmp | sed 1d; du -sh /srv/trader-v3/backups 2>/dev/null || true"

run "S-15 other writers of the watcher config tables (D1): db_manager.py callers" \
  "grep -rls 'db_manager' /etc/cron* /var/spool/cron /etc/systemd/system 2>/dev/null || echo 'no cron/systemd reference'; ps -eo pid,etime,cmd | grep -E 'db_manager\\.py' | grep -v grep || echo 'no running db_manager'"

run "S-16 hermes-feeder direct reader of the real DB (unchanged by O-0)" \
  "systemctl show trader-v3-hermes-feeder -p ActiveState,User,EnvironmentFiles; systemctl cat trader-v3-hermes-feeder | grep -E '^(ExecStart|Environment=|EnvironmentFile)' | $REDACT"

run "S-17 recent error baseline (1h): operator-query journal, watcher logs, caddy journal (counts only)" \
  "echo \"operator-query errors: \$(journalctl -u trader-v3-controlplane-operator-query --since -1h --no-pager | grep -ciE 'error|traceback' || true)\"; echo \"watcher db failures: \$(docker logs --since 1h $WATCHER_CONTAINER 2>&1 | grep -c '\\[db\\] Failed' || true)\"; echo \"watcher exits: \$(docker logs --since 24h $WATCHER_CONTAINER 2>&1 | grep -ciE 'exitWatcher|exiting' || true)\"; echo \"caddy errors: \$(journalctl -u caddy --since -1h --no-pager | grep -ci '\"level\":\"error\"' || true)\""

run "S-18 host-local HTTP probes without credentials (watcher is still unauthenticated before W-0 ships)" \
  "for u in http://127.0.0.1:9090/api/status http://127.0.0.1:9090/healthz http://127.0.0.1:8183/v1/accounts http://127.0.0.1:8183/v1/watcher/status; do printf '%s ' \$u; curl -s -o /dev/null -w '%{http_code}\n' -m 5 \$u; done"

if [ "$MODE" = "plan" ]; then
  printf '\nPLAN_ONLY: nothing was executed. Run on jp-24 only after the user authorizes O0-A01.\n'
else
  printf '\nO0_SITE_CHECK_DONE at=%s (read-only; no file written on the host)\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
fi
