#!/usr/bin/env bash
# Offline tests for review wac-032-r2 🟡-1 and 🟡-2.
#
# Part 1 (plan structure): parses the PLAN output of every gate/apply/build phase and pins
#   the step ORDER that the earlier rounds fixed: preflight/build start by clearing the old
#   gate; apply starts with REQUIRE <gate>; fleet baseline before the first write; automatic
#   rollback armed before the first live write; RUNTIME_REPLACEMENT_BEGINS right before the
#   restart/recreate; fleet guard after it (60 s settle, 4 samples, 20 s); the migration
#   dry-run stops the container gracefully (docker stop -t) and analyses the backup-API copy.
# Part 2 (automatic rollback, sandbox): runs the REAL apply phases with --execute inside
#   O0_SANDBOX (refused on any host that has /srv/trader-v3) with stubbed id, docker,
#   systemctl, curl, caddy, install, chown, stat, journalctl. For each of Caddy, watcher and
#   operator-query: (A) a failure BEFORE the restart/recreate must restore the files and must
#   NOT restart/recreate anything; (B) a failure AFTER it must restore and restart/recreate.
#   In both cases the fleet guard must run after the rollback and its verdict be recorded.
# All values are fakes generated here; nothing leaves the temporary directory.
# --structure-only runs part 1 only (auth_gate_test.sh calls it that way).
set -eo pipefail
STRUCTURE_ONLY=0; [ "${1:-}" != "--structure-only" ] || STRUCTURE_ONLY=1
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
O0="$(cd "$HERE/.." && pwd)"
REPO="$(cd "$O0/../../.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/o0-rollback.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
fails=0; checks=0
ok() { checks=$((checks + 1)); echo "PASS $*"; }
bad() { checks=$((checks + 1)); fails=$((fails + 1)); echo "FAIL $*"; }

# ---------------------------------------------------------------- 1. plan structure
plan() { env -u O0_FLEET_SETTLE_S -u O0_FLEET_SAMPLES -u O0_FLEET_INTERVAL_S bash "$O0/$1" --phase "$2" > "$WORK/plan-${1%.sh}-$2.txt"; }
for pair in o0_deploy_caddy.sh:preflight o0_deploy_caddy.sh:apply o0_deploy_watcher.sh:preflight o0_deploy_watcher.sh:build \
            o0_deploy_watcher.sh:apply o0_deploy_operator_query.sh:preflight o0_deploy_operator_query.sh:apply; do
  plan "${pair%%:*}" "${pair#*:}"
done
if python3 - "$WORK" > "$WORK/structure.txt" 2>&1 <<'PY'; then ok "plan structure: $(tail -1 "$WORK/structure.txt")"; else bad "plan structure"; cat "$WORK/structure.txt"; fi
import re, sys
from pathlib import Path
work = Path(sys.argv[1])

def events(name):
    out = []
    for line in (work / name).read_text(encoding="utf-8").splitlines():
        m = re.match(r"^PLAN \d+  (.*)$", line)
        n = re.match(r"^NOTE     (.*)$", line)
        if m:
            out.append(["PLAN", m.group(1), ""])
        elif n:
            out.append(["NOTE", n.group(1), ""])
        elif out:
            out[-1][2] += line + "\n"
    return out

def idx(ev, pred, what):
    for i, e in enumerate(ev):
        if pred(e):
            return i
    raise AssertionError(f"missing: {what}")

problems = []
def check(cond, msg):
    if not cond:
        problems.append(msg)

for name, stage in (("plan-o0_deploy_caddy-preflight.txt", "caddy-preflight"), ("plan-o0_deploy_watcher-preflight.txt", "watcher-preflight"),
                    ("plan-o0_deploy_watcher-build.txt", "watcher-build"), ("plan-o0_deploy_operator_query-preflight.txt", "oq-preflight")):
    ev = [e for e in events(name) if e[0] == "PLAN"]
    check(ev and ev[0][1].startswith(f"clear any earlier {stage} gate") and f"{stage}.gate.json" in ev[0][2],
          f"{name}: first step must clear the old {stage} gate (got {ev[0][1][:50] if ev else None!r})")
    check("record the passed" in ev[-1][1] and stage in ev[-1][1] + ev[-1][2], f"{name}: last step must write the {stage} gate")

apply_cases = {
    "caddy": ("plan-o0_deploy_caddy-apply.txt", "caddy-preflight", "before-caddy", "backup Caddyfile", "add WATCHER_BROWSER_PROXY_TOKEN",
              "restart caddy", "after-caddy"),
    "watcher": ("plan-o0_deploy_watcher-apply.txt", "watcher-preflight", "before-watcher", "backups:", "install the watcher env_file",
                "recreate watcher with the tested image", "after-watcher"),
    "oq": ("plan-o0_deploy_operator_query-apply.txt", "oq-preflight", "before-oq", "backup overwritten files", "add WATCHER_GATEWAY_TOKEN",
           "restart operator-query ONLY", "after-oq"),
}
for key, (name, gate, base, backup, first_write, restart, after) in apply_cases.items():
    ev = events(name)
    plans = [e for e in ev if e[0] == "PLAN"]
    check(plans[0][1].startswith(f"REQUIRE passed {gate} gate"), f"{key}: apply must start with REQUIRE {gate}")
    i_base = idx(ev, lambda e: e[1].startswith(f"fleet baseline ({base})"), f"{key} fleet baseline")
    i_backup = idx(ev, lambda e: e[1].startswith(backup), f"{key} backup")
    i_armed = idx(ev, lambda e: e[0] == "NOTE" and e[1].startswith(f"AUTO_ROLLBACK_ARMED"), f"{key} rollback armed")
    i_write = idx(ev, lambda e: e[1].startswith(first_write), f"{key} first live write")
    i_mark = idx(ev, lambda e: e[0] == "NOTE" and e[1].startswith("RUNTIME_REPLACEMENT_BEGINS"), f"{key} runtime mark")
    i_restart = idx(ev, lambda e: e[0] == "PLAN" and e[1].startswith(restart), f"{key} restart")
    i_after = idx(ev, lambda e: e[1].startswith(f"fleet after {after}:"), f"{key} fleet after")
    check(i_base < i_backup < i_armed < i_write, f"{key}: order must be fleet baseline < backup < rollback armed < first live write "
          f"(got {i_base},{i_backup},{i_armed},{i_write})")
    check(all(e[0] == "PLAN" and not e[1].startswith("fleet") for e in ev[1:i_base] if e[0] == "PLAN") and i_base < i_write,
          f"{key}: nothing but gates before the fleet baseline")
    check(i_mark == i_restart - 1, f"{key}: RUNTIME_REPLACEMENT_BEGINS must come immediately before '{restart}' ({i_mark} vs {i_restart})")
    check(i_after > i_restart, f"{key}: fleet guard must follow the restart")
    check("wait 60s, then 4 samples every 20s" in ev[i_after][1], f"{key}: fleet guard defaults changed: {ev[i_after][1]!r}")
    check(sum(1 for e in ev if e[0] == "PLAN" and e[1].startswith(restart)) == 1, f"{key}: exactly one restart step in apply")

build = events("plan-o0_deploy_watcher-build.txt")
text = "".join(e[2] for e in build)
dry = next(e for e in build if e[1].startswith("first-start migration dry-run"))
check("docker stop -t 30 o0-watcher-dryrun" in dry[2], "build: the dry-run container must be stopped gracefully (docker stop -t)")
check("docker kill" not in text and "SIGKILL" not in text, "build: no docker kill / SIGKILL anywhere in build")
check(dry[2].index("docker rm -f o0-watcher-dryrun") < dry[2].index("docker run -d --name o0-watcher-dryrun"),
      "build: rm -f only as stale cleanup BEFORE docker run")
post = next(e for e in build if e[1].startswith("post-migration copy via the backup API"))
m = re.search(r"sqlite-backup --source '([^']+)/dryrun\.db' --dest '([^']+)/dryrun\.post\.db'", post[2])
check(m is not None and m.group(1) == m.group(2), "build: dryrun.post.db must be produced by sqlite-backup from dryrun.db")
check("baseline --db '" in post[2] and "/dryrun.post.db' --export-json" in post[2], "build: the post-migration analysis must read dryrun.post.db")
check(not re.search(r"\bcp\b[^\n]*dryrun\.post\.db", post[2]), "build: dryrun.post.db must not be a plain cp")

if problems:
    print("\n".join(problems))
    sys.exit(1)
print("STRUCTURE_OK preflight_gate_clear=4 apply_order=3 build_graceful_stop=ok build_backup_api=ok")
PY

# ---------------------------------------------------------------- 2. automatic rollback (sandbox)
if [ "$STRUCTURE_ONLY" = 1 ]; then :
elif [ -e /srv/trader-v3 ]; then echo "SKIP sandbox part: this host has /srv/trader-v3"; else
BIN="$WORK/bin"; mkdir -p "$BIN"
printf '#!/usr/bin/env bash\n[ "$1" = "-u" ] && echo 0 || /usr/bin/id "$@"\n' > "$BIN/id"
cat > "$BIN/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
case "$*" in
  *psql*) echo 'account-a ACTIVE abcdef012345 hb_age=1.0' ;;
  "image inspect"*) echo "sha256:cand" ;;
  "inspect -f {{.Image}}"*) echo "sha256:running" ;;
  "inspect -f {{.RestartCount}}"*) echo "0 true" ;;
  *" config --quiet"*) [ -z "${DC_CONFIG_FAIL:-}" ] || exit 1 ;;
  logs*) printf 'Web UI listening on 0.0.0.0:9100\n'; [ -z "${WATCHER_LOG_DBFAIL:-}" ] || printf '[db] Failed to open trading db\n' ;;
esac
exit 0
STUB
cat > "$BIN/systemctl" <<'STUB'
#!/usr/bin/env bash
echo "systemctl $*" >> "$CALLS"
if [ "$1" = "is-active" ]; then
  n=$(cat "$CALLS.active" 2>/dev/null || echo 0); echo $((n + 1)) > "$CALLS.active"
  if [ -n "${ACTIVE_FAIL_FIRST:-}" ] && [ "$n" = 0 ]; then exit 3; fi
fi
exit 0
STUB
cat > "$BIN/curl" <<'STUB'
#!/usr/bin/env bash
case "$*" in *"/v1/accounts"*) printf 401 ;; *) printf 200 ;; esac
STUB
cat > "$BIN/caddy" <<'STUB'
#!/usr/bin/env bash
echo "caddy $1" >> "$CALLS"
case "$1" in
  adapt) [ -z "${CADDY_ADAPT_FAIL:-}" ] || exit 1; cat "$GOOD_ADAPTED" ;;
  version) echo v2.8.4 ;;
esac
exit 0
STUB
cat > "$BIN/install" <<'STUB'
#!/usr/bin/env python3
import os, shutil, sys
if os.environ.get("INSTALL_FAIL"):
    sys.exit(1)
args, files, mk, i = sys.argv[1:], [], False, 0
while i < len(args):
    if args[i] == "-D":
        mk = True
    elif args[i] in ("-m", "-o", "-g"):
        i += 1
    else:
        files.append(args[i])
    i += 1
src, dst = files
if mk:
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
shutil.copyfile(src, dst)
STUB
printf '#!/usr/bin/env bash\nexit 0\n' > "$BIN/chown"
printf '#!/usr/bin/env bash\necho "600 root:root ${@: -1}"\n' > "$BIN/stat"
cat > "$BIN/journalctl" <<'STUB'
#!/usr/bin/env bash
[ -z "${OQ_JOURNAL_FAIL:-}" ] || echo 'watcher gateway: route artifact unavailable'
exit 0
STUB
chmod +x "$BIN"/*
GOOD_ADAPTED="$WORK/good-adapted.json"
python3 - "$O0" "$REPO/contracts/generated/caddy-watcher-gateway-paths.txt" "$GOOD_ADAPTED" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[1])
import o0_caddy_watcher_routes as r
from pathlib import Path
lines, _ = r.load_list(Path(sys.argv[2]))
Path(sys.argv[3]).write_text(json.dumps(r._fixture(lines)), encoding="utf-8")
PY
export GOOD_ADAPTED

new_sandbox() {  # new_sandbox <name>  -> SB, S, CALLS
  SB="$WORK/sb-$1"; S="$SB/srv/trader-staging/o0-20260926T000000Z"; CALLS="$WORK/calls-$1.log"
  rm -rf "$SB"; : > "$CALLS"; rm -f "$CALLS.active"
  mkdir -p "$SB/srv/trader-v3" "$S/bundle/tools" "$S/bundle/caddy" "$S/evidence" "$S/creds"
  cp "$O0"/o0_*.py "$S/bundle/tools/"
  cp "$REPO/contracts/generated/caddy-watcher-gateway-paths.txt" "$S/bundle/caddy/"
  printf '{"candidate": "%040d", "deploy_candidate": true, "phase_max": "P2", "yaml_sha256": "x"}\n' 7 > "$S/bundle/RELEASE.json"
  python3 "$O0/o0_watcher_credentials.py" generate --out-dir "$S/creds/set-initial" --allow-any-dir >/dev/null
}
seal_bundle() { (cd "$S/bundle" && find . -type f ! -name SHA256SUMS | sed 's|^\./||' | sort | xargs sha256sum > SHA256SUMS); }
run_apply() {  # run_apply <script> <auth> args...  -> sets RC, OUT
  local script="$1" auth="$2"; shift 2
  RC=0
  OUT="$(PATH="$BIN:$PATH" O0_SANDBOX="$SB" CALLS="$CALLS" O0_FLEET_NODES=account-a O0_FLEET_READY_PORTS=8081 O0_FLEET_SETTLE_S=0 \
         O0_FLEET_SAMPLES=1 O0_FLEET_INTERVAL_S=0 bash "$O0/$script" --execute --phase apply --auth-id "$auth" --stage-dir "$S" "$@" 2>&1)" || RC=$?
}
fake_catalog() {  # fake_catalog <file>: catalog tokens are random fakes, distinct from the watcher set
  python3 -c 'import secrets,sys; open(sys.argv[1],"w").write("RISK_ADMIN_TOKEN=%s\nSYSTEM_OBSERVER_TOKEN=%s\n" % (secrets.token_urlsafe(32), secrets.token_urlsafe(32)))' "$1"
}
assert_rollback() {  # assert_rollback <label> <want runtime_rolled_back yes|no> <restart-regex> <want count> <failing step prefix>
  local label="$1" want="$2" re="$3" count="$4" step="$5" n failed_at
  if [ "$RC" -eq 0 ]; then bad "$label: apply should have failed"; printf '%s\n' "$OUT" | tail -8 | sed 's/^/    /'; return; fi
  # the failure must be the injected one (not an earlier, accidental one)
  failed_at="$(printf '%s\n' "$OUT" | awk '/\[o0\] STEP [0-9]+: /{s=$0} /apply failed: automatic rollback/{print s; exit}' | sed -E 's/.*STEP [0-9]+: //')"
  case "$failed_at" in "$step"*) ok "$label: failed at the injected step ($step)" ;;
    *) bad "$label: failed at '$failed_at', not at '$step'"; printf '%s\n' "$OUT" | grep -E 'STEP|FAIL|ABORT' | tail -6 | sed 's/^/    /' ;; esac
  n=$(grep -cE "$re" "$CALLS" || true)
  [ "$n" = "$count" ] && ok "$label: '$re' called $n time(s)" || { bad "$label: '$re' called $n time(s), want $count"; printf '%s\n' "$OUT" | tail -12 | sed 's/^/    /'; }
  if grep -q "runtime_rolled_back=$want rollback_rc=0 fleet_rc=0" "$S/evidence/auto-rollback.log" 2>/dev/null; then ok "$label: auto-rollback.log records runtime_rolled_back=$want, rollback ok, fleet verdict 0"
  else bad "$label: auto-rollback.log"; cat "$S/evidence/auto-rollback.log" 2>/dev/null | sed 's/^/    /'; printf '%s\n' "$OUT" | tail -12 | sed 's/^/    /'; fi
  if grep -q 'FLEET_UNCHANGED' "$S/evidence/fleet-after-auto-rollback-1.verdict.txt" 2>/dev/null && printf '%s' "$OUT" | grep -q 'AUTO_ROLLBACK_DONE'; then
    ok "$label: fleet guard ran after the rollback and its verdict is in evidence"
  else bad "$label: no fleet guard verdict after the rollback"; fi
}

# ---- Caddy
caddy_setup() {
  new_sandbox "caddy-$1"
  mkdir -p "$SB/etc/caddy" "$S/caddy"
  printf 'old.example {\n\trespond 200\n}\n' > "$SB/etc/caddy/Caddyfile"
  printf 'CADDY_DOMAIN=jp-bot.balen.wang\n' > "$SB/etc/caddy/v3.env"
  printf 'jp-bot.balen.wang {\n\trespond 204\n}\n' > "$S/caddy/Caddyfile.candidate"
  (cd "$SB/etc/caddy" && sha256sum "$SB/etc/caddy/Caddyfile" "$SB/etc/caddy/v3.env") > "$S/evidence/caddy-live.sha256"
  seal_bundle
  python3 "$O0/o0_tool.py" gate-write --out "$S/evidence/caddy-preflight.gate.json" --stage caddy-preflight --bundle "$S/bundle" \
    --file-sha "candidate_caddyfile=$S/caddy/Caddyfile.candidate" --file-sha "live_caddyfile=$SB/etc/caddy/Caddyfile" \
    --file-sha "live_caddy_env=$SB/etc/caddy/v3.env" --file-sha "cred_caddy_env=$S/creds/set-initial/caddy.env" >/dev/null
}
caddy_restored() { sha256sum -c --quiet "$S/evidence/caddy-live.sha256" >/dev/null 2>&1 && ok "$1: Caddyfile and v3.env restored byte for byte" || bad "$1: Caddy files not restored"; }

caddy_setup A
CADDY_ADAPT_FAIL=1 run_apply o0_deploy_caddy.sh O0-A05
assert_rollback "caddy A (adapt fails before restart)" no 'systemctl restart' 0 "adapt the INSTALLED file"
caddy_restored "caddy A"
caddy_setup B
ACTIVE_FAIL_FIRST=1 run_apply o0_deploy_caddy.sh O0-A05
assert_rollback "caddy B (not active after restart)" yes 'systemctl restart caddy' 2 "caddy active"
caddy_restored "caddy B"
# C: restart happened, then the rollback itself fails at its sha check (the restored bytes are
# not the recorded ones). Fail-fast: no validate, NO second restart with unknown files; the
# failure is recorded and the fleet guard still runs.
caddy_setup C
echo "0000000000000000000000000000000000000000000000000000000000000000  $SB/etc/caddy/Caddyfile" > "$S/evidence/caddy-live.sha256"
ACTIVE_FAIL_FIRST=1 run_apply o0_deploy_caddy.sh O0-A05
n_restart=$(grep -c 'systemctl restart caddy' "$CALLS" || true); n_validate=$(grep -c 'caddy validate' "$CALLS" || true)
if [ "$RC" -ne 0 ] && [ "$n_restart" = 1 ] && [ "$n_validate" = 2 ] && grep -q 'runtime_rolled_back=no rollback_rc=[1-9][0-9]* fleet_rc=0' "$S/evidence/auto-rollback.log" \
   && printf '%s' "$OUT" | grep -q 'ROLLBACK FAILED'; then
  ok "caddy C (rollback sha check fails): stops at once, no validate, no second restart, ROLLBACK FAILED + fleet verdict recorded"
else bad "caddy C: rc=$RC restarts=$n_restart validates=$n_validate"; cat "$S/evidence/auto-rollback.log" 2>/dev/null | sed 's/^/    /'; fi

# ---- watcher
watcher_setup() {
  new_sandbox "watcher-$1"
  W="$SB/srv/trader"; SRC="$W/services/telegram-watcher"; DB="$SB/var/lib/docker/volumes/trader_signal-data/_data/watcher-trading.db"
  mkdir -p "$SRC/lib" "$(dirname "$DB")" "$S/bundle/watcher/lib" "$S/bundle/compose" "$SB/srv/trader-secrets" "$SB/srv/trader-v3/secrets/control-plane"
  echo 'old server' > "$SRC/server.js"; echo 'services: {old: {}}' > "$W/docker-compose.yml"
  echo 'new server' > "$S/bundle/watcher/server.js"; echo 'module.exports = {};' > "$S/bundle/watcher/lib/auth.js"
  echo 'services: {new: {}}' > "$S/bundle/compose/docker-compose.yml"
  python3 -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute("CREATE TABLE account_configs (account_id TEXT)"); c.commit()' "$DB"
  fake_catalog "$SB/srv/trader-v3/secrets/control-plane/operator-query.env"
  printf 'server.js\nlib/auth.js\n' > "$WORK/wpaths"; echo docker-compose.yml > "$WORK/cpaths"
  python3 "$O0/o0_tool.py" manifest-build --root "$SRC" --paths-file "$WORK/wpaths" --out "$S/bundle/watcher.baseline.sha256" >/dev/null
  python3 "$O0/o0_tool.py" manifest-build --root "$S/bundle/watcher" --paths-file "$WORK/wpaths" --out "$S/bundle/watcher.candidate.sha256" >/dev/null
  python3 "$O0/o0_tool.py" manifest-build --root "$W" --paths-file "$WORK/cpaths" --out "$S/bundle/compose.baseline.sha256" >/dev/null
  python3 "$O0/o0_tool.py" manifest-build --root "$S/bundle/compose" --paths-file "$WORK/cpaths" --out "$S/bundle/compose.candidate.sha256" >/dev/null
  echo MIGRATION_DRYRUN_OK > "$S/evidence/watcher-dryrun.log"
  seal_bundle
  python3 "$O0/o0_tool.py" gate-write --out "$S/evidence/watcher-preflight.gate.json" --stage watcher-preflight --bundle "$S/bundle" \
    --file-sha "cred_watcher_env=$S/creds/set-initial/watcher.env" --file-sha "live_compose=$W/docker-compose.yml" >/dev/null
  python3 "$O0/o0_tool.py" gate-write --out "$S/evidence/watcher-build.gate.json" --stage watcher-build --bundle "$S/bundle" \
    --field cand_image_id=sha256:cand --file-sha "dryrun_log=$S/evidence/watcher-dryrun.log" >/dev/null
}
watcher_restored() {
  if python3 "$O0/o0_tool.py" manifest-verify --root "$SRC" --manifest "$S/bundle/watcher.baseline.sha256" >/dev/null \
     && python3 "$O0/o0_tool.py" manifest-verify --root "$W" --manifest "$S/bundle/compose.baseline.sha256" >/dev/null \
     && [ ! -e "$SB/srv/trader-secrets/watcher-gateway.env" ]; then ok "$1: source, compose and env_file state restored"; else bad "$1: watcher files not restored"; fi
}
watcher_args=(--watcher-root "$WORK/sb-watcher-X/srv/trader" --env-target "$WORK/sb-watcher-X/srv/trader-secrets/watcher-gateway.env")
watcher_setup A
DC_CONFIG_FAIL=1 run_apply o0_deploy_watcher.sh O0-A07 "${watcher_args[@]//sb-watcher-X/sb-watcher-A}"
assert_rollback "watcher A (compose config fails before recreate)" no 'docker compose .* up ' 0 "compose config parses"
watcher_restored "watcher A"
watcher_setup B
WATCHER_LOG_DBFAIL=1 run_apply o0_deploy_watcher.sh O0-A07 "${watcher_args[@]//sb-watcher-X/sb-watcher-B}"
assert_rollback "watcher B (startup check fails after recreate)" yes 'docker compose .* up ' 2 "startup:"
watcher_restored "watcher B"

# ---- operator-query
oq_setup() {
  new_sandbox "oq-$1"
  CP="$SB/srv/trader-v3/services/control-plane"; OQE="$SB/srv/trader-v3/secrets/control-plane/operator-query.env"
  mkdir -p "$CP/api" "$S/bundle/controlplane/api/generated" "$(dirname "$OQE")" "$SB/srv/trader-v3/.venv-cp/bin"
  printf '#!/usr/bin/env bash\necho "python 3.12 httpx stub"\n' > "$SB/srv/trader-v3/.venv-cp/bin/python"; chmod +x "$SB/srv/trader-v3/.venv-cp/bin/python"
  echo 'old = 1' > "$CP/api/read_api.py"
  echo 'new = 1' > "$S/bundle/controlplane/api/read_api.py"; echo 'PAYLOAD = {}' > "$S/bundle/controlplane/api/generated/watcher_gateway_routes.py"
  fake_catalog "$OQE"
  printf 'api/read_api.py\napi/generated/watcher_gateway_routes.py\n' > "$WORK/opaths"
  python3 "$O0/o0_tool.py" manifest-build --root "$CP" --paths-file "$WORK/opaths" --out "$S/bundle/controlplane.baseline.sha256" >/dev/null
  cp "$S/bundle/controlplane.baseline.sha256" "$S/bundle/controlplane-context.baseline.sha256"
  python3 "$O0/o0_tool.py" manifest-build --root "$S/bundle/controlplane" --paths-file "$WORK/opaths" --out "$S/bundle/controlplane.candidate.sha256" >/dev/null
  sha256sum "$OQE" > "$WORK/oq-env-$1.sha256"
  seal_bundle
  python3 "$O0/o0_tool.py" gate-write --out "$S/evidence/oq-preflight.gate.json" --stage oq-preflight --bundle "$S/bundle" \
    --file-sha "cred_oq_env=$S/creds/set-initial/operator-query.env" --file-sha "live_oq_env=$OQE" >/dev/null
}
oq_restored() {
  if python3 "$O0/o0_tool.py" manifest-verify --root "$CP" --manifest "$S/bundle/controlplane.baseline.sha256" >/dev/null \
     && sha256sum -c --quiet "$WORK/oq-env-$2.sha256" >/dev/null 2>&1; then ok "$1: control-plane code and operator-query.env restored"; else bad "$1: operator-query files not restored"; fi
}
oq_setup A
INSTALL_FAIL=1 run_apply o0_deploy_operator_query.sh O0-A08 --cp-root "$WORK/sb-oq-A/srv/trader-v3/services/control-plane" \
  --operator-query-env "$WORK/sb-oq-A/srv/trader-v3/secrets/control-plane/operator-query.env"
assert_rollback "operator-query A (install fails before restart)" no 'systemctl restart' 0 "install the five reviewed files"
oq_restored "operator-query A" A
oq_setup B
OQ_JOURNAL_FAIL=1 run_apply o0_deploy_operator_query.sh O0-A08 --cp-root "$WORK/sb-oq-B/srv/trader-v3/services/control-plane" \
  --operator-query-env "$WORK/sb-oq-B/srv/trader-v3/secrets/control-plane/operator-query.env"
assert_rollback "operator-query B (startup journal fails after restart)" yes 'systemctl restart trader-v3-controlplane-operator-query' 2 "startup journal since the restart"
oq_restored "operator-query B" B
# the other control-plane units are never touched, in any case
if grep -hE 'systemctl .*(node-control|event-ingest)' "$WORK"/calls-*.log >/dev/null; then bad "a rollback touched node-control/event-ingest"; else ok "node-control/event-ingest never touched"; fi
fi

if [ "$fails" -gt 0 ]; then echo "APPLY_ROLLBACK_TEST_FAILED failures=$fails checks=$checks"; exit 1; fi
echo "APPLY_ROLLBACK_TEST_OK checks=$checks"
