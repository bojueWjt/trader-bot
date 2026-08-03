#!/usr/bin/env bash
# 2026-08-03 fix deployment (repo commit 6ecad66 / bundle 8b880f6) — run as root ON hk.
#   scp -r <payload> root@100.104.27.123:/srv/trader-v3/staging-20260803-6ecad66
#   ssh root@100.104.27.123 'bash /srv/trader-v3/staging-20260803-6ecad66/deploy.sh'
# Ships: container-patches delta (contracts OM models, planner ownership marker,
# strategy -2021 TP market fallback, node.py), lifecycle monitor alerts,
# control-plane read_api replay fix, trade-outcomes unit portability fix.
# Fail-closed: any error leaves nodes HALTED and prints rollback commands.
# SKIP_RESUME=1 to deploy without resuming.
set -Eeuo pipefail

T=/srv/trader-v3
STAGING="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TAG="deploy-${STAMP}-6ecad66"
BACKUP_ROOT="$T/backups/$TAG"
LOCK=/var/lock/trader-v3-deploy.lock

exec 9>"$LOCK"
flock -n 9 || { echo "FATAL: another deploy holds $LOCK"; exit 1; }

die() { echo "FATAL: $*" >&2; exit 1; }
on_err() {
  echo "!! deploy FAILED — nodes remain HALTED (fail-closed)." >&2
  echo "!! rollback: for f in \"$BACKUP_ROOT\"/files/*; do cat \"\$f\" > \"\$(sed -n \"s|^\$(basename \"\$f\")\t||p\" \"$BACKUP_ROOT/index.tsv\")\"; done" >&2
  echo "!! then: systemctl daemon-reload; systemctl restart \$CP_UNIT \$LC_UNIT; docker restart trader-v3-node-a trader-v3-node-b; RESUME manually." >&2
}
trap on_err ERR

# ---------- preflight ----------
cd "$STAGING"
[ -f SHA256SUMS ] || die "SHA256SUMS missing in staging"
sha256sum -c SHA256SUMS >/dev/null || die "staging payload integrity check failed"
[ -f bundle-manifest.json ] || die "bundle-manifest.json missing"
command -v python3 >/dev/null || die "python3 missing"
[ -x "$T/.venv-cp/bin/python" ] || die "$T/.venv-cp/bin/python missing"
[ -f "$T/.env.v3" ] || die "$T/.env.v3 missing"

# no-regression guards for the 08-03 live hotfix lineage
grep -q 'CANCEL_ORDER' contracts.py || die "staged contracts.py lost CANCEL_ORDER"
grep -q 'class Execution(' contracts.py || die "staged contracts.py lost OM models"
grep -q 'if not order.tags' intent_execution_planner.py || die "staged planner lost ownership-marker hotfix"
grep -q '_submit_immediate_tp_market_fallback' intent_execution_strategy.py || die "staged strategy lost -2021 fallback"

# host-side targets
MON_TGT="$T/scripts/order_lifecycle_monitor.py"
API_TGT="$T/services/control-plane/api/read_api.py"
UNIT_TGT="/etc/systemd/system/trader-v3-trade-outcomes.service"
for f in host/order_lifecycle_monitor.py host/read_api.py host/trader-v3-trade-outcomes.service; do
  [ -f "$f" ] || die "staging missing $f"
done
[ -f "$MON_TGT" ] || die "live monitor not found at $MON_TGT"
[ -f "$API_TGT" ] || die "live read_api not found at $API_TGT"
[ -f "$UNIT_TGT" ] || die "live unit not found at $UNIT_TGT"

# service unit discovery (fail-closed on ambiguity)
CP_UNIT="$(systemctl list-units 'trader-v3-*' --all --no-legend --plain | awk '{print $1}' | grep -E 'controlplane|control-plane' | head -1)"
LC_UNIT="$(systemctl list-units 'trader-v3-*' --all --no-legend --plain | awk '{print $1}' | grep -E 'lifecycle' | head -1)"
[ -n "$CP_UNIT" ] || die "control-plane unit not found"
[ -n "$LC_UNIT" ] || die "lifecycle monitor unit not found"
docker inspect trader-v3-node-a trader-v3-node-b >/dev/null || die "node containers missing"

# changed-set vs live container-patches (no NEW files allowed)
CHANGED_CONTAINER=()
while IFS=$'\t' read -r bundle_path; do
  live="$T/container-patches/$bundle_path"
  [ -f "$live" ] || die "live container-patch missing: $live (unexpected NEW file)"
  if ! cmp -s "$bundle_path" "$live"; then CHANGED_CONTAINER+=("$bundle_path"); fi
done < <(python3 -c "import json;[print(f['bundle_path']) for f in json.load(open('bundle-manifest.json'))['files']]")

CHANGED_HOST=()
cmp -s host/order_lifecycle_monitor.py "$MON_TGT" || CHANGED_HOST+=("monitor")
cmp -s host/read_api.py "$API_TGT" || CHANGED_HOST+=("read_api")
cmp -s host/trader-v3-trade-outcomes.service "$UNIT_TGT" || CHANGED_HOST+=("unit")

echo "== changed container files: ${CHANGED_CONTAINER[*]:-none}"
echo "== changed host files: ${CHANGED_HOST[*]:-none}"
[ "${#CHANGED_CONTAINER[@]}" -gt 0 ] || [ "${#CHANGED_HOST[@]}" -gt 0 ] || { echo "nothing to deploy"; exit 0; }

# ---------- HALT ----------
"$T/.venv-cp/bin/python" - "$T/.env.v3" <<'PY'
from datetime import datetime, timezone
import json, sys, time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4
import psycopg2

def read_environment(path):
    values = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in {"'", '"'}:
            v = v[1:-1]
        values[k.strip()] = v
    return values

env = read_environment(sys.argv[1])
db, token = env.get("DATABASE_URL", ""), env.get("RISK_ADMIN_TOKEN", "")
if not db or not token:
    raise SystemExit("DATABASE_URL/RISK_ADMIN_TOKEN missing")
conn = psycopg2.connect(db); conn.autocommit = True
try:
    with conn.cursor() as cur:
        cur.execute("SELECT node_id FROM node_heartbeats WHERE last_seen_at >= now() - interval '5 minutes' ORDER BY node_id")
        nodes = [r[0] for r in cur.fetchall()]
    if len(nodes) != 2:
        raise SystemExit(f"expected two fresh node heartbeats, found {nodes}")
    rid = f"deploy-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}-halt"
    body = json.dumps({"type": "HALT", "reason": "2026-08-03 audited fix deployment (6ecad66)",
                       "confirm": True, "request_id": rid, "target_nodes": nodes, "scope": {}}).encode()
    req = Request("http://127.0.0.1:8080/v1/commands", data=body, method="POST",
                  headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "X-Request-Id": rid})
    try:
        with urlopen(req, timeout=15) as r:
            cid = json.load(r).get("command_id")
    except (HTTPError, URLError) as e:
        raise SystemExit(f"HALT request failed: {e}")
    if not cid:
        raise SystemExit("HALT command id missing")
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        states = {}
        for port in (8081, 8082):
            try:
                with urlopen(f"http://127.0.0.1:{port}/ready", timeout=5) as r:
                    states[port] = json.load(r).get("trading_state")
            except (OSError, URLError, ValueError) as e:
                states[port] = str(e)
        with conn.cursor() as cur:
            cur.execute("SELECT node_id, status FROM command_node_acks WHERE command_id=%s", (cid,))
            acks = cur.fetchall()
            cur.execute("SELECT status FROM operator_commands WHERE command_id=%s", (cid,))
            row = cur.fetchone()
        if set(states.values()) == {"HALTED"} and len(acks) == 2 \
           and all(s == "acked" for _, s in acks) and row and row[0] == "completed":
            print(f"HALT acked by {nodes}; command_id={cid}")
            raise SystemExit(0)
        time.sleep(2)
    raise SystemExit(f"HALT acceptance timed out: {states}")
finally:
    conn.close()
PY

# ---------- backup ----------
[ ! -e "$BACKUP_ROOT" ] || die "backup path exists: $BACKUP_ROOT"
mkdir -p "$BACKUP_ROOT/files"; chmod 0700 "$BACKUP_ROOT"
: > "$BACKUP_ROOT/index.tsv"
bk() { # bk <src> <label>
  cp -a "$1" "$BACKUP_ROOT/files/$2"
  printf '%s\t%s\n' "$2" "$1" >> "$BACKUP_ROOT/index.tsv"
}
for f in "${CHANGED_CONTAINER[@]:-}"; do [ -n "$f" ] && bk "$T/container-patches/$f" "cp__$f"; done
for h in "${CHANGED_HOST[@]:-}"; do
  case "$h" in
    monitor) bk "$MON_TGT" "host__order_lifecycle_monitor.py" ;;
    read_api) bk "$API_TGT" "host__read_api.py" ;;
    unit) bk "$UNIT_TGT" "host__trader-v3-trade-outcomes.service" ;;
  esac
done
( cd "$BACKUP_ROOT/files" && sha256sum * > ../SHA256SUMS )
cp bundle-manifest.json "$BACKUP_ROOT/deployed-bundle-manifest.json"
echo "== backup at $BACKUP_ROOT"

# ---------- install (in-place cat: preserves bind-mount inodes) ----------
for f in "${CHANGED_CONTAINER[@]:-}"; do
  [ -n "$f" ] || continue
  cat "$f" > "$T/container-patches/$f"
  cmp -s "$f" "$T/container-patches/$f" || die "post-install mismatch: $f"
done
for h in "${CHANGED_HOST[@]:-}"; do
  case "$h" in
    monitor) cat host/order_lifecycle_monitor.py > "$MON_TGT" ;;
    read_api) cat host/read_api.py > "$API_TGT" ;;
    unit) cat host/trader-v3-trade-outcomes.service > "$UNIT_TGT"; systemctl daemon-reload ;;
  esac
done
echo "6ecad66 fix(execution): land hotfix lineage, -2021 TP fallback, ops alerts (deployed $STAMP)" > "$T/DEPLOYED_COMMIT.txt"
echo "== files installed"

# ---------- restart & verify ----------
systemctl restart "$CP_UNIT"
systemctl restart "$LC_UNIT"
docker restart trader-v3-node-a trader-v3-node-b >/dev/null
for u in "$CP_UNIT" "$LC_UNIT"; do
  sleep 2; systemctl is-active --quiet "$u" || die "$u failed to restart"
done

# in-container content check via mount targets
python3 - <<'PY'
import json, subprocess, sys
man = json.load(open("bundle-manifest.json"))
for node in ("trader-v3-node-a", "trader-v3-node-b"):
    for f in man["files"]:
        out = subprocess.run(["docker", "exec", node, "sha256sum", f["mount_target"]],
                             capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            sys.exit(f"{node}: cannot hash {f['mount_target']}: {out.stderr.strip()[:120]}")
        if out.stdout.split()[0] != f["sha256"]:
            sys.exit(f"{node}: {f['mount_target']} sha mismatch (bind-mount inode broken?)")
print("in-container content verified on both nodes")
PY

# nodes back to ready (HALTED/startup expected)
for i in $(seq 1 45); do
  ok=1
  for port in 8081 8082; do
    state="$(curl -sf "http://127.0.0.1:$port/ready" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("ready"), d.get("trading_state"))' 2>/dev/null || echo bad)"
    case "$state" in "True HALTED") ;; *) ok=0 ;; esac
  done
  [ "$ok" = 1 ] && break
  sleep 4
done
[ "$ok" = 1 ] || die "nodes did not reach ready+HALTED after restart"
echo "== both nodes ready (HALTED/startup)"

# journal error scan (30s window)
sleep 5
if journalctl -u "$CP_UNIT" --since "-2 min" --no-pager 2>/dev/null | grep -qiE "traceback|validationerror"; then
  die "control-plane logging errors after restart"
fi

# ---------- RESUME ----------
if [ "${SKIP_RESUME:-0}" = "1" ]; then
  echo "== SKIP_RESUME=1: leaving nodes HALTED. Resume manually when ready."
else
  "$T/.venv-cp/bin/python" - "$T/.env.v3" <<'PY'
from datetime import datetime, timezone
import json, sys, time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4
import psycopg2

def read_environment(path):
    values = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in {"'", '"'}:
            v = v[1:-1]
        values[k.strip()] = v
    return values

env = read_environment(sys.argv[1])
db, token = env.get("DATABASE_URL", ""), env.get("RISK_ADMIN_TOKEN", "")
conn = psycopg2.connect(db); conn.autocommit = True
try:
    with conn.cursor() as cur:
        cur.execute("SELECT node_id FROM node_heartbeats WHERE last_seen_at >= now() - interval '5 minutes' ORDER BY node_id")
        nodes = [r[0] for r in cur.fetchall()]
    if len(nodes) != 2:
        raise SystemExit(f"expected two fresh node heartbeats, found {nodes}")
    rid = f"deploy-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}-resume"
    body = json.dumps({"type": "RESUME", "reason": "2026-08-03 fix deployment verified: -2021 TP fallback + ops alerts (6ecad66)",
                       "confirm": True, "request_id": rid, "target_nodes": nodes, "scope": {}}).encode()
    req = Request("http://127.0.0.1:8080/v1/commands", data=body, method="POST",
                  headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "X-Request-Id": rid})
    try:
        with urlopen(req, timeout=15) as r:
            cid = json.load(r).get("command_id")
    except (HTTPError, URLError) as e:
        raise SystemExit(f"RESUME request failed: {e}")
    if not cid:
        raise SystemExit("RESUME command id missing")
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        states = {}
        for port in (8081, 8082):
            try:
                with urlopen(f"http://127.0.0.1:{port}/ready", timeout=5) as r:
                    states[port] = json.load(r).get("trading_state")
            except (OSError, URLError, ValueError) as e:
                states[port] = str(e)
        with conn.cursor() as cur:
            cur.execute("SELECT node_id, status FROM command_node_acks WHERE command_id=%s", (cid,))
            acks = cur.fetchall()
        if set(states.values()) == {"ACTIVE"} and len(acks) == 2 and all(s == "acked" for _, s in acks):
            print(f"RESUME acked by {nodes}; command_id={cid}")
            raise SystemExit(0)
        time.sleep(2)
    raise SystemExit(f"RESUME acceptance timed out: {states}")
finally:
    conn.close()
PY
fi

echo "== DEPLOY OK  tag=$TAG  backup=$BACKUP_ROOT"
echo "== rollback (if needed later): see $BACKUP_ROOT/index.tsv + files/, restore with cat > target (inode-preserving), daemon-reload, restart $CP_UNIT $LC_UNIT, docker restart nodes"
