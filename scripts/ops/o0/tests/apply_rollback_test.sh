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
#   (D) the restart/recreate command itself fails; (E) the fleet guard after the restart sees a
#   change: stop and report, no automatic rollback; (F) a candidate manifest WITH an ABSENT line
#   (review wac-032-r3 §3.2/§3.3, 🟡-3).
# Part 2 also covers (wac-072): restore-db keeps the ORIGINAL owner/mode (inode-aware chown/stat
#   stubs), recovers to the original DB files when a step after the stop fails, refuses a rerun
#   over set-aside files (r2 🟡-5); the stage O isolation gate refuses apply before any write
#   (r2 🟡-6); the fleet guard parameters are checked against the node heartbeat parameters in
#   a real execute run (r2 🟡-7).
# wac-060: stage C installs the WGW-1.0.2 snippet file next to the Caddyfile: SN1 its install
#   fails (no restart, removed again), SN2 it pre-existed with the same bytes (restored, not
#   removed), SN3 a different file sits there (refused at the gate before any write). The
#   reviewer's five restore-db failure points from wac-073 (R073 STOPFAIL/STARTFAIL/MVMID/
#   WALMADE/RECSHA, review wac-072 🟡-4) are part of this file.
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
            o0_deploy_watcher.sh:apply o0_deploy_watcher.sh:restore-db o0_deploy_operator_query.sh:preflight o0_deploy_operator_query.sh:apply; do
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

# stage O isolation gate (review wac-032-r2 🟡-6): in preflight before the gate is written, in
# apply before the fleet baseline and the first backup; both other units named
for name, before_what in (("plan-o0_deploy_operator_query-preflight.txt", "record the passed oq-preflight gate"),
                          ("plan-o0_deploy_operator_query-apply.txt", "fleet baseline (before-oq)")):
    ev = events(name)
    i_iso = idx(ev, lambda e: e[1].startswith("control-plane unit isolation (S-10)"), f"{name} isolation gate")
    i_next = idx(ev, lambda e: e[1].startswith(before_what), f"{name} {before_what}")
    check(i_iso < i_next, f"{name}: the isolation gate must come before '{before_what}'")
    check("cp-isolation" in ev[i_iso][2] and "--other-unit trader-v3-controlplane-node-control" in ev[i_iso][2]
          and "--other-unit trader-v3-controlplane-event-ingest" in ev[i_iso][2], f"{name}: isolation gate must check both other units")

# restore-db (review wac-032-r2 🟡-5): owner/mode/sha recorded before the stop; recovery armed
# and the runtime marked before the stop; the install uses the RECORDED owner and mode
ev = events("plan-o0_deploy_watcher-restore-db.txt")
i_rec = idx(ev, lambda e: e[1].startswith("record owner, mode and sha of the live DB files"), "restore-db owner record")
i_arm = idx(ev, lambda e: e[0] == "NOTE" and e[1].startswith("AUTO_ROLLBACK_ARMED (watcher-restore-db)"), "restore-db recovery armed")
i_mark = idx(ev, lambda e: e[0] == "NOTE" and e[1].startswith("RUNTIME_REPLACEMENT_BEGINS"), "restore-db runtime mark")
i_stop = idx(ev, lambda e: e[1] == "stop watcher", "restore-db stop")
i_inst = idx(ev, lambda e: e[1].startswith("set aside the current DB files"), "restore-db install")
check(i_rec < i_arm < i_mark < i_stop < i_inst, f"restore-db order: record {i_rec} < armed {i_arm} < mark {i_mark} < stop {i_stop} < install {i_inst}")
check("stat -c '%u:%g %a'" in ev[i_rec][2] and "DB_REPLACED_NOT_EMPTY" in ev[i_rec][2], "restore-db: owner/mode recorded with stat, rerun refused")
check('chown "$own"' in ev[i_inst][2] and 'chmod "$mode"' in ev[i_inst][2] and "root:root" not in ev[i_inst][2], "restore-db: install must use the recorded owner/mode")

if problems:
    print("\n".join(problems))
    sys.exit(1)
print("STRUCTURE_OK preflight_gate_clear=4 apply_order=3 restore_db_order=ok oq_isolation_gate=2 build_graceful_stop=ok build_backup_api=ok")
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
  *psql*) if [ -n "${HALT_AFTER_RESTART:-}" ] && grep -qE 'systemctl restart|compose .* up ' "$CALLS"; then echo 'account-a HALTED abcdef012345 hb_age=1.0'; else echo 'account-a ACTIVE abcdef012345 hb_age=1.0'; fi ;;
  *" stop "*) if [ -n "${STOP_FAIL_FIRST:-}" ] && [ ! -e "$CALLS.sf" ]; then : > "$CALLS.sf"; exit 1; fi ;;
  *" start "*) if [ -n "${START_FAIL_FIRST:-}" ] && [ ! -e "$CALLS.stf" ]; then : > "$CALLS.stf"; exit 1; fi
     if [ -n "${CORRUPT_REPLACED:-}" ] && [ ! -e "$CALLS.cr" ]; then : > "$CALLS.cr"; printf 'x' >> "$CORRUPT_REPLACED"; fi
     if [ -n "${START_MAKES_WAL:-}" ] && [ ! -e "$CALLS.wal" ]; then : > "$CALLS.wal"; printf 'walbytes' > "$START_MAKES_WAL"; fi ;;
  *" up "*) if [ -n "${UP_FAIL_FIRST:-}" ] && [ ! -e "$CALLS.uf" ]; then : > "$CALLS.uf"; exit 1; fi ;;
  "image inspect"*) echo "sha256:cand" ;;
  "inspect -f {{.Image}}"*) echo "sha256:running" ;;
  "inspect -f {{.RestartCount}}"*) echo "0 true" ;;
  *" config --quiet"*) [ -z "${DC_CONFIG_FAIL:-}" ] || exit 1 ;;
  logs*) printf 'Web UI listening on 0.0.0.0:9100\n[watcher] Connected, listening...\n'; [ -z "${WATCHER_LOG_DBFAIL:-}" ] || printf '[db] Failed to open trading db\n'
         # restore-db: the FIRST start (on the restored copy) logs a DB failure
         if [ -n "${RESTORE_DB_FAIL:-}" ] && [ "$(grep -cE 'compose .* start ' "$CALLS")" = 1 ]; then printf '[db] Failed to open trading db\n'; fi ;;
esac
exit 0
STUB
cat > "$BIN/systemctl" <<'STUB'
#!/usr/bin/env bash
echo "systemctl $*" >> "$CALLS"
# unit properties for the stage O isolation gate (o0_tool.py cp-isolation); ISOLATION_FAIL
# injects the two S-10 violations (env file / Environment= name) with a sentinel VALUE
if [ "$1" = "show" ] && [[ " $* " == *" -p LoadState "* ]]; then
  echo LoadState=loaded; echo "WorkingDirectory=$O0_SANDBOX/srv/trader-v3/services/control-plane/api"
  # wac-060 (review wac-072 🟡-3): reload = node-control drop-in changed on disk but not loaded;
  # unparsed = an EnvironmentFiles= value the gate cannot read
  if [ "${ISOLATION_FAIL:-}" = reload ] && [[ "$2" == *node-control ]]; then echo NeedDaemonReload=yes; else echo NeedDaemonReload=no; fi
  if [ "${ISOLATION_FAIL:-}" = unparsed ] && [[ "$2" == *event-ingest ]]; then echo 'EnvironmentFiles=etc/relative.env (ignore_errors=no)'; fi
  oqe="$O0_SANDBOX/srv/trader-v3/secrets/control-plane/operator-query.env"
  case "$2" in
    *operator-query) echo "EnvironmentFiles=$oqe (ignore_errors=no)"; echo 'Environment=PYTHONPATH=/x' ;;
    *node-control) [ "${ISOLATION_FAIL:-}" != envfile ] || echo "EnvironmentFiles=$oqe (ignore_errors=no)"; echo 'Environment=' ;;
    *event-ingest) if [ "${ISOLATION_FAIL:-}" = envname ]; then echo 'Environment=WATCHER_SNAPSHOT_TOKEN=SENTINELisoleak0123456789'; else echo 'Environment='; fi ;;
  esac
  exit 0
fi
if [ "$1" = "restart" ] && [ -n "${RESTART_FAIL_FIRST:-}" ] && [ ! -e "$CALLS.rf" ]; then : > "$CALLS.rf"; exit 1; fi
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
# chown/stat: ownership is kept per INODE in $CALLS.owners (mv keeps it, cp makes a new file
# owned by root), so a restore that forgets to re-apply the recorded owner is visible.
cat > "$BIN/chown" <<'STUB'
#!/usr/bin/env python3
import json, os, sys
if os.environ.get("CHOWN_FAIL"):
    sys.exit(1)
db = os.environ["CALLS"] + ".owners"
owners = json.load(open(db)) if os.path.exists(db) else {}
owner = sys.argv[1].replace("root", "0")
for f in sys.argv[2:]:
    owners[str(os.stat(f).st_ino)] = owner
json.dump(owners, open(db, "w"))
STUB
# cp/mv: real, plus two restore-db faults. CP_CORRUPT: the copy onto the watcher DB lands with
# different bytes. MV_LOSE_OWNER: a cross-filesystem move that does not keep the owner/mode
# (copy to a new inode, remove the source).
cat > "$BIN/cp" <<'STUB'
#!/usr/bin/env bash
/bin/cp "$@" || exit $?
if [ -n "${CP_CORRUPT:-}" ]; then last="${*: -1}"; case "$last" in *watcher-trading.db) printf 'x' >> "$last" ;; esac; fi
STUB
cat > "$BIN/mv" <<'STUB'
#!/usr/bin/env bash
if [ -n "${MV_FAIL_SRC:-}" ] && [ ! -e "$CALLS.mvf" ]; then for a in "$@"; do case "$a" in *"$MV_FAIL_SRC") : > "$CALLS.mvf"; exit 1;; esac; done; fi
[ -n "${MV_LOSE_OWNER:-}" ] || exec /bin/mv "$@"
args=(); for a in "$@"; do [ "$a" = "--" ] || args+=("$a"); done
src="${args[0]}"; dst="${args[1]}"; [ -d "$dst" ] && dst="$dst/$(basename "$src")"
/bin/cp "$src" "$dst.mvtmp" && /bin/rm -f "$src" && /bin/mv "$dst.mvtmp" "$dst"
STUB
cat > "$BIN/stat" <<'STUB'
#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
fmt = args[args.index("-c") + 1]
files = [a for i, a in enumerate(args) if a != "-c" and (i == 0 or args[i - 1] != "-c")]
db = os.environ.get("CALLS", "/nonexistent") + ".owners"
owners = json.load(open(db)) if os.path.exists(db) else {}
for f in files:
    st = os.stat(f)
    uid, gid = owners.get(str(st.st_ino), "0:0").split(":")
    name = lambda n: "root" if n == "0" else n
    out = fmt.replace("%a", format(st.st_mode & 0o7777, "o")).replace("%u", uid).replace("%g", gid)
    print(out.replace("%U", name(uid)).replace("%G", name(gid)).replace("%n", f))
STUB
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
  cp "$REPO/contracts/generated/caddy-watcher-gateway-paths.txt" "$REPO/contracts/generated/caddy-watcher-gateway.caddy" "$S/bundle/caddy/"
  printf '{"candidate": "%040d", "deploy_candidate": true, "phase_max": "P2", "yaml_sha256": "x"}\n' 7 > "$S/bundle/RELEASE.json"
  python3 "$O0/o0_watcher_credentials.py" generate --out-dir "$S/creds/set-initial" --allow-any-dir >/dev/null
}
seal_bundle() { (cd "$S/bundle" && find . -type f ! -name SHA256SUMS | sed 's|^\./||' | sort | xargs sha256sum > SHA256SUMS); }
run_apply() {  # run_apply <script> <auth> args...  -> sets RC, OUT
  local script="$1" auth="$2"; shift 2
  RC=0
  OUT="$(PATH="$BIN:$PATH" O0_SANDBOX="$SB" O0_FLEET_PARAMS_SANDBOX_SKIP=1 CALLS="$CALLS" O0_FLEET_NODES=account-a O0_FLEET_READY_PORTS=8081 O0_FLEET_SETTLE_S=0 \
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
  printf 'import caddy-watcher-gateway.caddy\njp-bot.balen.wang {\n\timport watcher_gateway_routes\n\trespond 204\n}\n' > "$S/caddy/Caddyfile.candidate"
  cp "$S/bundle/caddy/caddy-watcher-gateway.caddy" "$S/caddy/caddy-watcher-gateway.caddy"   # preflight stages it (relative import)
  (cd "$SB/etc/caddy" && sha256sum "$SB/etc/caddy/Caddyfile" "$SB/etc/caddy/v3.env") > "$S/evidence/caddy-live.sha256"
  seal_bundle
  python3 "$O0/o0_tool.py" gate-write --out "$S/evidence/caddy-preflight.gate.json" --stage caddy-preflight --bundle "$S/bundle" \
    --file-sha "candidate_caddyfile=$S/caddy/Caddyfile.candidate" --file-sha "live_caddyfile=$SB/etc/caddy/Caddyfile" \
    --file-sha "live_caddy_env=$SB/etc/caddy/v3.env" --file-sha "cred_caddy_env=$S/creds/set-initial/caddy.env" \
    --file-sha "staged_snippet=$S/caddy/caddy-watcher-gateway.caddy" >/dev/null
}
caddy_restored() {  # caddy_restored <label> [present]: snippet file removed (first deploy) or back to the pre-apply bytes
  sha256sum -c --quiet "$S/evidence/caddy-live.sha256" >/dev/null 2>&1 && ok "$1: Caddyfile and v3.env restored byte for byte" || bad "$1: Caddy files not restored"
  if [ "${2:-}" = present ]; then
    cmp -s "$SB/etc/caddy/caddy-watcher-gateway.caddy" "$S/bundle/caddy/caddy-watcher-gateway.caddy" && ok "$1: pre-existing snippet file restored" || bad "$1: pre-existing snippet file not restored"
  else
    [ ! -e "$SB/etc/caddy/caddy-watcher-gateway.caddy" ] && ok "$1: snippet file removed again (did not exist before apply)" || bad "$1: snippet file left behind"
  fi
}

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
# SN1 (wac-060): installing the snippet file fails (first write to /etc/caddy): files restored, no restart
caddy_setup SN1
INSTALL_FAIL=1 run_apply o0_deploy_caddy.sh O0-A05
assert_rollback "caddy SN1 (snippet install fails)" no 'systemctl restart' 0 "install the watcher gateway snippet"
caddy_restored "caddy SN1"
# SN2 (wac-060): the snippet file already exists with the SAME bytes (re-run); failure after the restart
caddy_setup SN2
cp "$S/bundle/caddy/caddy-watcher-gateway.caddy" "$SB/etc/caddy/caddy-watcher-gateway.caddy"
ACTIVE_FAIL_FIRST=1 run_apply o0_deploy_caddy.sh O0-A05
assert_rollback "caddy SN2 (snippet pre-existing, not active after restart)" yes 'systemctl restart caddy' 2 "caddy active"
caddy_restored "caddy SN2" present
# SN3 (wac-060): a DIFFERENT file already sits at the snippet path: refused before any write
caddy_setup SN3
printf '(watcher_gateway_routes) {\n}\n' > "$SB/etc/caddy/caddy-watcher-gateway.caddy"
cp "$SB/etc/caddy/caddy-watcher-gateway.caddy" "$WORK/foreign-snippet"
run_apply o0_deploy_caddy.sh O0-A05
if [ "$RC" -ne 0 ] && [ ! -e "$S/backup-caddy" ] && ! grep -q 'systemctl restart' "$CALLS" && cmp -s "$WORK/foreign-snippet" "$SB/etc/caddy/caddy-watcher-gateway.caddy" \
   && sha256sum -c --quiet "$S/evidence/caddy-live.sha256" >/dev/null 2>&1 && ! printf '%s' "$OUT" | grep -q AUTO_ROLLBACK_ARMED; then
  ok "caddy SN3 (a different snippet file is live): refused at the gate, nothing backed up, written or restarted"
else bad "caddy SN3: rc=$RC"; printf '%s\n' "$OUT" | grep -E 'STEP|FAIL' | tail -5 | sed 's/^/    /'; fi

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
# ---- review wac-032-r3 §3.2/§3.3 (🟡-3): D = the restart/recreate command itself fails; E = the fleet guard after
# the restart sees a change (stop and report, no automatic rollback); F = candidate manifest WITH an ABSENT line
# restart command ITSELF fails (marker set before it): rollback restarts once more
caddy_setup D
RESTART_FAIL_FIRST=1 run_apply o0_deploy_caddy.sh O0-A05
assert_rollback "caddy D (systemctl restart itself fails)" yes 'systemctl restart caddy' 2 "restart caddy"
caddy_restored "caddy D"
# fleet guard after the restart sees a change: no automatic rollback, exit non-zero, one restart
caddy_setup E
HALT_AFTER_RESTART=1 run_apply o0_deploy_caddy.sh O0-A05
n=$(grep -c 'systemctl restart caddy' "$CALLS" || true)
if [ "$RC" -ne 0 ] && [ "$n" = 1 ] && [ ! -e "$S/evidence/auto-rollback.log" ] && ! printf '%s' "$OUT" | grep -q 'apply failed: automatic rollback'; then
  ok "caddy E (fleet guard after restart = CHANGED): rc=$RC, 1 restart, no auto rollback (stop and report)"
else bad "caddy E: rc=$RC restarts=$n"; printf '%s\n' "$OUT" | tail -6 | sed 's/^/    /'; fi
printf '%s' "$OUT" | grep -q 'FLEET_GUARD_STOP sample=1 rc=3' && ok "caddy E: the guard's stop line is printed (report to the user; no RESUME, no rollback)" || bad "caddy E: no FLEET_GUARD_STOP rc=3 line"
# watcher: recreate command itself fails
watcher_setup D
UP_FAIL_FIRST=1 run_apply o0_deploy_watcher.sh O0-A07 "${watcher_args[@]//sb-watcher-X/sb-watcher-D}"
assert_rollback "watcher D (compose up itself fails)" yes 'docker compose .* up ' 2 "recreate watcher with the tested image"
watcher_restored "watcher D"
# watcher: candidate manifest WITH an ABSENT line (a whitelisted file the candidate drops)
watcher_setup F
echo 'legacy' > "$SRC/lib/old.js"
printf 'server.js\nlib/auth.js\nlib/old.js\n' > "$WORK/wpaths"
python3 "$O0/o0_tool.py" manifest-build --root "$SRC" --paths-file "$WORK/wpaths" --out "$S/bundle/watcher.baseline.sha256" >/dev/null
python3 "$O0/o0_tool.py" manifest-build --root "$S/bundle/watcher" --paths-file "$WORK/wpaths" --out "$S/bundle/watcher.candidate.sha256" >/dev/null
grep -q '^ABSENT  lib/old.js$' "$S/bundle/watcher.candidate.sha256" && ok "watcher F: candidate manifest has ABSENT lib/old.js" || bad "watcher F: no ABSENT line"
seal_bundle
python3 "$O0/o0_tool.py" gate-write --out "$S/evidence/watcher-preflight.gate.json" --stage watcher-preflight --bundle "$S/bundle" \
  --file-sha "cred_watcher_env=$S/creds/set-initial/watcher.env" --file-sha "live_compose=$W/docker-compose.yml" >/dev/null
python3 "$O0/o0_tool.py" gate-write --out "$S/evidence/watcher-build.gate.json" --stage watcher-build --bundle "$S/bundle" \
  --field cand_image_id=sha256:cand --file-sha "dryrun_log=$S/evidence/watcher-dryrun.log" >/dev/null
DC_CONFIG_FAIL=1 run_apply o0_deploy_watcher.sh O0-A07 "${watcher_args[@]//sb-watcher-X/sb-watcher-F}"
assert_rollback "watcher F (ABSENT line, fails at compose config)" no 'docker compose .* up ' 0 "compose config parses"
printf '%s' "$OUT" | grep -q 'watcher-live-vs-candidate' && ok "watcher F: candidate manifest verified (old.js was removed)" || bad "watcher F: no candidate verify"
[ "$(cat "$SRC/lib/old.js" 2>/dev/null)" = legacy ] && ok "watcher F: rollback restored lib/old.js" || bad "watcher F: lib/old.js not restored"
watcher_restored "watcher F"
# operator-query: restart command itself fails
oq_setup D
RESTART_FAIL_FIRST=1 run_apply o0_deploy_operator_query.sh O0-A08 --cp-root "$WORK/sb-oq-D/srv/trader-v3/services/control-plane" \
  --operator-query-env "$WORK/sb-oq-D/srv/trader-v3/secrets/control-plane/operator-query.env"
assert_rollback "operator-query D (restart itself fails)" yes 'systemctl restart trader-v3-controlplane-operator-query' 2 "restart operator-query ONLY"
oq_restored "operator-query D" D
# ---- restore-db (review wac-032-r2 🟡-5): the restored file keeps the ORIGINAL owner and mode;
# a failure after the stop puts the original DB files back (owner/mode/sha proven), starts the
# watcher again and records the fleet verdict; a second run over set-aside files is refused.
rdb_setup() {
  new_sandbox "rdb-$1"
  DBD="$SB/var/lib/docker/volumes/trader_signal-data/_data"; DB="$DBD/watcher-trading.db"; W="$SB/srv/trader"
  mkdir -p "$DBD" "$W" "$SB/srv/trader-v3/secrets/control-plane" "$S/backup-watcher"
  fake_catalog "$SB/srv/trader-v3/secrets/control-plane/operator-query.env"
  echo 'services: {watcher: {}}' > "$W/docker-compose.yml"
  python3 -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute("CREATE TABLE t (v TEXT)"); c.execute("INSERT INTO t VALUES (\"post-apply\")"); c.commit()' "$DB"
  printf 'shm-bytes' > "$DB-shm"
  chmod 640 "$DB" "$DB-shm"; CALLS="$CALLS" "$BIN/chown" 1000:1000 "$DB" "$DB-shm"
  python3 -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute("CREATE TABLE t (v TEXT)"); c.execute("INSERT INTO t VALUES (\"pre-apply\")"); c.commit()' "$S/backup-watcher/watcher-trading.pre-o0.db"
  sha256sum "$S/backup-watcher/watcher-trading.pre-o0.db" > "$S/evidence/watcher-db-backup.sha256"
  ORIG_DB_SHA="$(sha256sum < "$DB" | cut -d' ' -f1)"; ORIG_SHM_SHA="$(sha256sum < "$DB-shm" | cut -d' ' -f1)"
  PRE_SHA="$(sha256sum < "$S/backup-watcher/watcher-trading.pre-o0.db" | cut -d' ' -f1)"
}
run_restore() {
  RC=0
  OUT="$(PATH="$BIN:$PATH" O0_SANDBOX="$SB" O0_FLEET_PARAMS_SANDBOX_SKIP=1 CALLS="$CALLS" O0_FLEET_NODES=account-a O0_FLEET_READY_PORTS=8081 O0_FLEET_SETTLE_S=0 \
         O0_FLEET_SAMPLES=1 O0_FLEET_INTERVAL_S=0 bash "$O0/o0_deploy_watcher.sh" --execute --phase restore-db --auth-id O0-A07R --i-understand-data-loss \
         --stage-dir "$S" --watcher-root "$W" --telegram-timeout-s 5 2>&1)" || RC=$?
}
own_of() { CALLS="$CALLS" "$BIN/stat" -c '%u:%g %a' "$1"; }
original_back() {  # the live DB is the ORIGINAL (post-apply) file again, owner and mode unchanged
  if [ "$(sha256sum < "$DB" | cut -d' ' -f1)" = "$ORIG_DB_SHA" ] && [ "$(sha256sum < "$DB-shm" | cut -d' ' -f1)" = "$ORIG_SHM_SHA" ] \
     && [ "$(own_of "$DB")" = "1000:1000 640" ] && [ "$(own_of "$DB-shm")" = "1000:1000 640" ] && [ -z "$(ls -A "$S/backup-watcher/db-replaced")" ]; then
    ok "$1: original DB files back (sha, owner 1000:1000, mode 640), db-replaced empty"
  else bad "$1: original DB not back: owner=$(own_of "$DB" 2>&1) replaced=$(ls "$S/backup-watcher/db-replaced" 2>&1 | tr '\n' ' ')"; printf '%s\n' "$OUT" | tail -8 | sed 's/^/    /'; fi
}
rdb_setup OK
run_restore
if [ "$RC" -eq 0 ] && [ "$(sha256sum < "$DB" | cut -d' ' -f1)" = "$PRE_SHA" ] && printf '%s' "$OUT" | grep -q RESTORED_DB_OWNER_MODE_OK; then
  ok "restore-db OK: live DB = pre-apply copy (RESTORED_DB_SHA_OK, RESTORED_DB_OWNER_MODE_OK)"
else bad "restore-db OK: rc=$RC"; printf '%s\n' "$OUT" | grep -E 'STEP|ABORT|MISMATCH|FAIL' | tail -6 | sed 's/^/    /'; fi
[ "$(own_of "$DB")" = "1000:1000 640" ] && [ "$(cat "$S/evidence/watcher-db-owner.txt")" = "1000:1000 640" ] \
  && ok "restore-db OK: restored file has the ORIGINAL owner and mode (1000:1000 640), recorded in evidence" || bad "restore-db OK: owner/mode $(own_of "$DB")"
[ -e "$S/backup-watcher/db-replaced/watcher-trading.db" ] && [ -e "$S/backup-watcher/db-replaced/watcher-trading.db-shm" ] && [ ! -e "$DB-shm" ] \
  && ok "restore-db OK: DB and -shm set aside in db-replaced/, no stale -shm next to the restored DB" || bad "restore-db OK: set-aside files"
[ "$(grep -cE 'compose .* stop ' "$CALLS")" = 1 ] && [ "$(grep -cE 'compose .* start ' "$CALLS")" = 1 ] && [ ! -e "$S/evidence/auto-rollback.log" ] \
  && ok "restore-db OK: one stop, one start, no automatic recovery" || bad "restore-db OK: stop/start counts"
# a second run over the set-aside files is refused before anything stops or moves
n_stop=$(grep -cE 'compose .* stop ' "$CALLS"); run_restore
if [ "$RC" -ne 0 ] && printf '%s' "$OUT" | grep -q DB_REPLACED_NOT_EMPTY && [ "$(grep -cE 'compose .* stop ' "$CALLS")" = "$n_stop" ] \
   && [ "$(sha256sum < "$DB" | cut -d' ' -f1)" = "$PRE_SHA" ] && [ ! -e "$S/evidence/auto-rollback.log" ]; then
  ok "restore-db rerun: refused (DB_REPLACED_NOT_EMPTY) before any stop; the set-aside originals are not overwritten"
else bad "restore-db rerun: rc=$RC"; fi
# the watcher fails on the restored copy -> automatic recovery to the original files
rdb_setup FAILUP
RESTORE_DB_FAIL=1 run_restore
assert_rollback "restore-db FAILUP (watcher fails on the restored DB)" yes 'docker compose .* start ' 2 "watcher up"
original_back "restore-db FAILUP"
[ "$(sha256sum < "$S/backup-watcher/db-failed-restore/watcher-trading.db" | cut -d' ' -f1)" = "$PRE_SHA" ] \
  && ok "restore-db FAILUP: the restored copy that failed is kept in db-failed-restore/" || bad "restore-db FAILUP: db-failed-restore"
printf '%s' "$OUT" | grep -q RESTORE_DB_RECOVERED_FILES && ok "restore-db FAILUP: RESTORE_DB_RECOVERED_FILES printed" || bad "restore-db FAILUP: no RESTORE_DB_RECOVERED_FILES"
# failure while installing (chown) after the originals moved: recovery before any start
rdb_setup FAILINSTALL
CHOWN_FAIL=1 run_restore
assert_rollback "restore-db FAILINSTALL (chown fails after the move)" yes 'docker compose .* start ' 1 "set aside the current DB files"
original_back "restore-db FAILINSTALL"
# the installed copy's bytes differ from the recorded sha: stop at the install step, recover
rdb_setup CORRUPT
CP_CORRUPT=1 run_restore
assert_rollback "restore-db CORRUPT (installed bytes differ from the recorded sha)" yes 'docker compose .* start ' 1 "set aside the current DB files"
printf '%s' "$OUT" | grep -q RESTORED_DB_SHA_MISMATCH && ok "restore-db CORRUPT: RESTORED_DB_SHA_MISMATCH named" || bad "restore-db CORRUPT: no RESTORED_DB_SHA_MISMATCH"
original_back "restore-db CORRUPT"
# the move does not keep owner/mode: recovery must NOT start the watcher on those files
rdb_setup MVOWNER
MV_LOSE_OWNER=1 RESTORE_DB_FAIL=1 run_restore
n_start=$(grep -cE 'compose .* start ' "$CALLS" || true)
if [ "$RC" -ne 0 ] && printf '%s' "$OUT" | grep -q 'ROLLBACK FAILED' && [ "$n_start" = 1 ] \
   && grep -q 'runtime_rolled_back=no rollback_rc=[1-9][0-9]* fleet_rc=0' "$S/evidence/auto-rollback.log" 2>/dev/null \
   && [ "$(sha256sum < "$DB" | cut -d' ' -f1)" = "$ORIG_DB_SHA" ]; then
  ok "restore-db MVOWNER: original bytes back but owner/mode differ -> ROLLBACK FAILED, watcher not started, fleet verdict recorded"
else bad "restore-db MVOWNER: rc=$RC starts=$n_start"; cat "$S/evidence/auto-rollback.log" 2>/dev/null | sed 's/^/    /'; printf '%s\n' "$OUT" | grep -E 'STEP|owner|ROLLBACK' | tail -6 | sed 's/^/    /'; fi

# ==== reviewer wac-073 extra restore-db failure points
rdb_setup STOPFAIL
STOP_FAIL_FIRST=1 run_restore
assert_rollback "R073 STOPFAIL (compose stop fails)" yes 'docker compose .* start ' 1 "stop watcher"
original_back "R073 STOPFAIL"
rdb_setup STARTFAIL
START_FAIL_FIRST=1 run_restore
assert_rollback "R073 STARTFAIL (compose start itself fails)" yes 'docker compose .* start ' 2 "start watcher"
original_back "R073 STARTFAIL"
rdb_setup MVMID
MV_FAIL_SRC=watcher-trading.db-shm run_restore
assert_rollback "R073 MVMID (set-aside mv of -shm fails after the DB moved)" yes 'docker compose .* start ' 1 "set aside the current DB files"
original_back "R073 MVMID"
rdb_setup WALMADE
START_MAKES_WAL="$DB-wal" RESTORE_DB_FAIL=1 run_restore
assert_rollback "R073 WALMADE (failed start on restored copy left a -wal)" yes 'docker compose .* start ' 2 "watcher up"
original_back "R073 WALMADE"
[ ! -e "$DB-wal" ] && [ -e "$S/backup-watcher/db-failed-restore/watcher-trading.db-wal" ] && ok "R073 WALMADE: stray -wal moved to db-failed-restore" || bad "R073 WALMADE: stray -wal left next to the original DB"
[ "$(grep -cE 'compose .* stop ' "$CALLS")" = 2 ] && ok "R073 WALMADE: recovery stopped the watcher again before moving files" || bad "R073 WALMADE: recovery did not stop the watcher before moving files"
rdb_setup RECSHA
# the set-aside original gets corrupted before recovery: recovery must refuse to start the watcher
RESTORE_DB_FAIL=1 CORRUPT_REPLACED="$S/backup-watcher/db-replaced/watcher-trading.db" run_restore
n_start=$(grep -cE 'compose .* start ' "$CALLS" || true)
if [ "$RC" -ne 0 ] && printf '%s' "$OUT" | grep -q 'ROLLBACK FAILED' && [ "$n_start" = 1 ] && grep -q 'runtime_rolled_back=no rollback_rc=[1-9]' "$S/evidence/auto-rollback.log"; then
  ok "R073 RECSHA: set-aside original corrupted -> sha check fails, ROLLBACK FAILED, watcher NOT started, fleet verdict recorded"
else bad "R073 RECSHA: rc=$RC starts=$n_start"; cat "$S/evidence/auto-rollback.log" 2>/dev/null; printf '%s\n' "$OUT" | grep -E 'STEP|FAILED|OK' | tail -8; fi
# ---- stage O isolation gate (review wac-032-r2 🟡-6): a violation refuses apply before ANY write;
# wac-060: so does an UNCOMPARABLE unit (NeedDaemonReload=yes, unparseable EnvironmentFiles=)
for kind in envfile envname reload unparsed; do
  case "$kind" in reload|unparsed) want_iso='CP_ISOLATION_UNCOMPARABLE' ;; *) want_iso='CP_ISOLATION_FAILED' ;; esac
  oq_setup "ISO-$kind"
  ISOLATION_FAIL=$kind run_apply o0_deploy_operator_query.sh O0-A08 --cp-root "$WORK/sb-oq-ISO-$kind/srv/trader-v3/services/control-plane" \
    --operator-query-env "$WORK/sb-oq-ISO-$kind/srv/trader-v3/secrets/control-plane/operator-query.env"
  failed_at="$(printf '%s\n' "$OUT" | awk '/\[o0\] STEP [0-9]+: /{s=$0} END{print s}' | sed -E 's/.*STEP [0-9]+: //')"
  if [ "$RC" -ne 0 ] && { [ "$want_iso" != CP_ISOLATION_FAILED ] || printf '%s' "$OUT" | grep -q 'ENVFILE_ISOLATION VIOLATION'; } && printf '%s' "$OUT" | grep -q "$want_iso" \
     && [ "${failed_at#control-plane unit isolation}" != "$failed_at" ] && [ ! -e "$S/backup-operator-query" ] && [ ! -e "$S/evidence/auto-rollback.log" ] \
     && ! grep -q 'systemctl restart' "$CALLS"; then
    ok "operator-query ISO-$kind: apply refused at the isolation gate, before backup/env/install/restart"
  else bad "operator-query ISO-$kind: rc=$RC failed_at='$failed_at'"; printf '%s\n' "$OUT" | grep -E 'STEP|ISOLATION|ABORT' | tail -5 | sed 's/^/    /'; fi
  oq_restored "operator-query ISO-$kind" "ISO-$kind"
  printf '%s' "$OUT" | grep -q SENTINELisoleak && bad "operator-query ISO-$kind: an Environment= VALUE was printed" || ok "operator-query ISO-$kind: no env value printed (names only)"
done
# ---- fleet guard parameters vs node heartbeat parameters (review wac-032-r2 🟡-7), checked in
# every execute run BEFORE any step (here: the standalone guard, runbook step R-2)
run_guard_params() {  # env assignments come from the caller
  RC=0
  OUT="$(PATH="$BIN:$PATH" O0_SANDBOX="$SB" CALLS="$CALLS" O0_FLEET_NODES=account-a O0_FLEET_READY_PORTS=8081 \
         bash "$O0/o0_fleet_guard.sh" --execute --phase R-2 --auth-id O0-A21 --stage-dir "$S" --action before 2>&1)" || RC=$?
}
new_sandbox params-default
run_guard_params
if [ "$RC" -eq 0 ] && printf '%s' "$OUT" | grep -q 'FLEET_PARAMS_OK' && grep -q 'FLEET_BASELINE_OK' "$S/evidence/fleet-R-2-before.verdict.txt" \
   && grep -q 'fleet_params=hb_interval=2,hb_timeout=15,max_hb_age=5,max_hb_jump=5,settle=60,samples=4x20' "$S/evidence/authorizations.log"; then
  ok "fleet params: defaults (2 s / 15 s -> 5, 5, 60, 4 x 20) pass in a real execute run and are recorded in authorizations.log"
else bad "fleet params default: rc=$RC"; printf '%s\n' "$OUT" | tail -5 | sed 's/^/    /'; fi
new_sandbox params-timeout60
O0_NODE_HB_TIMEOUT_S=60 run_guard_params
if [ "$RC" -ne 0 ] && printf '%s' "$OUT" | grep -q 'FLEET_PARAMS_INCONSISTENT' && [ ! -e "$S/evidence/fleet-R-2-before.txt" ] && ! grep -q psql "$CALLS"; then
  ok "fleet params: node timeout 60 s with the default 60 s settle window is refused before any sample"
else bad "fleet params timeout60: rc=$RC"; printf '%s\n' "$OUT" | tail -5 | sed 's/^/    /'; fi
new_sandbox params-interval4
O0_NODE_HB_INTERVAL_S=4 run_guard_params
[ "$RC" -ne 0 ] && printf '%s' "$OUT" | grep -q 'O0_FLEET_MAX_HB_AGE=10' && ok "fleet params: interval 4 s with the default 5 s age is refused and 10 is suggested" || bad "fleet params interval4: rc=$RC"
new_sandbox params-interval4-adjusted
O0_NODE_HB_INTERVAL_S=4 O0_FLEET_MAX_HB_AGE=10 O0_FLEET_MAX_HB_JUMP=10 run_guard_params
if [ "$RC" -eq 0 ] && grep -q 'FLEET_BASELINE_OK .* max_hb_age=10.0' "$S/evidence/fleet-R-2-before.verdict.txt"; then
  ok "fleet params: a consistent non-default set (interval 4 s, age/jump 10) passes and reaches fleet-compare"
else bad "fleet params interval4-adjusted: rc=$RC"; printf '%s\n' "$OUT" | tail -5 | sed 's/^/    /'; fi
new_sandbox params-skip-needs-sandbox
if ( unset O0_SANDBOX; source "$O0/o0_common.sh"; O0_FLEET_PARAMS_SANDBOX_SKIP=1 O0_NODE_HB_TIMEOUT_S=60; O0_STAGE_DIR="$S"; o0_fleet_params_check ) >/dev/null 2>&1; then
  bad "fleet params: O0_FLEET_PARAMS_SANDBOX_SKIP skipped the check without O0_SANDBOX"
else ok "fleet params: the skip seam is ignored without O0_SANDBOX"; fi
# the other control-plane units are never touched, in any case
if grep -hE 'systemctl (restart|stop|start|kill|reload|try-restart|reload-or-restart) .*(node-control|event-ingest)' "$WORK"/calls-*.log >/dev/null; then bad "a rollback touched node-control/event-ingest"; else ok "node-control/event-ingest never touched (read-only 'systemctl show' only)"; fi
fi

if [ "$fails" -gt 0 ]; then echo "APPLY_ROLLBACK_TEST_FAILED failures=$fails checks=$checks"; exit 1; fi
echo "APPLY_ROLLBACK_TEST_OK checks=$checks"
