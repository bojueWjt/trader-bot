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
# wac-092 (review wac-090 🟡-5): the Caddy PREFLIGHT phase executed in the sandbox (PF1..PF8): all markers and the gate
#   (then apply accepts that gate), R13 snippet sha binding, candidate sha binding, R16 candidate / staged-snippet
#   symlinks and the resolved-path layer, G29 a different live snippet (LIVE_SNIPPET_DIFFERS), an identical one;
#   with O0_CADDY_BIN also PF-REAL: real Caddy validate + adapt of the prodlike fixture (gate written) and of the
#   review n08 '//' injection (verify refuses, no gate).
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
            o0_deploy_watcher.sh:apply o0_deploy_watcher.sh:restore-db o0_deploy_watcher_gateway.sh:preflight o0_deploy_watcher_gateway.sh:apply \
            o0_deploy_watcher_gateway.sh:rollback; do
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
                    ("plan-o0_deploy_watcher-build.txt", "watcher-build"), ("plan-o0_deploy_watcher_gateway-preflight.txt", "wgw-preflight")):
    ev = [e for e in events(name) if e[0] == "PLAN"]
    check(ev and ev[0][1].startswith(f"clear any earlier {stage} gate") and f"{stage}.gate.json" in ev[0][2],
          f"{name}: first step must clear the old {stage} gate (got {ev[0][1][:50] if ev else None!r})")
    check("record the passed" in ev[-1][1] and stage in ev[-1][1] + ev[-1][2], f"{name}: last step must write the {stage} gate")

apply_cases = {
    "caddy": ("plan-o0_deploy_caddy-apply.txt", "caddy-preflight", "before-caddy", "backup Caddyfile", "add WATCHER_BROWSER_PROXY_TOKEN",
              "restart caddy", "after-caddy"),
    "watcher": ("plan-o0_deploy_watcher-apply.txt", "watcher-preflight", "before-watcher", "backups:", "install the watcher env_file",
                "recreate watcher with the tested image", "after-watcher"),
    # WGW-1.0.4 stage O: nothing existing is overwritten (no backup step); the first write creates the new user; the runtime
    # replacement is "enable --no-reload" (then start)
    "wgw": ("plan-o0_deploy_watcher_gateway-apply.txt", "wgw-preflight", "before-wgw", None, "create the system user",
            "enable watcher-gateway WITHOUT a reload", "after-wgw"),
}
for key, (name, gate, base, backup, first_write, restart, after) in apply_cases.items():
    ev = events(name)
    plans = [e for e in ev if e[0] == "PLAN"]
    check(plans[0][1].startswith(f"REQUIRE passed {gate} gate"), f"{key}: apply must start with REQUIRE {gate}")
    i_base = idx(ev, lambda e: e[1].startswith(f"fleet baseline ({base})"), f"{key} fleet baseline")
    i_backup = idx(ev, lambda e: e[1].startswith(backup), f"{key} backup") if backup else i_base + 1
    i_armed = idx(ev, lambda e: e[0] == "NOTE" and e[1].startswith(f"AUTO_ROLLBACK_ARMED"), f"{key} rollback armed")
    i_write = idx(ev, lambda e: e[1].startswith(first_write), f"{key} first live write")
    i_mark = idx(ev, lambda e: e[0] == "NOTE" and e[1].startswith("RUNTIME_REPLACEMENT_BEGINS"), f"{key} runtime mark")
    i_restart = idx(ev, lambda e: e[0] == "PLAN" and e[1].startswith(restart), f"{key} restart")
    i_after = idx(ev, lambda e: e[1].startswith(f"fleet after {after}:"), f"{key} fleet after")
    check(i_base < i_backup <= i_armed < i_write, f"{key}: order must be fleet baseline < backup < rollback armed < first live write "
          f"(got {i_base},{i_backup},{i_armed},{i_write})")
    check(all(e[0] == "PLAN" and not e[1].startswith("fleet") for e in ev[1:i_base] if e[0] == "PLAN") and i_base < i_write,
          f"{key}: nothing but gates before the fleet baseline")
    check(i_mark == i_restart - 1, f"{key}: RUNTIME_REPLACEMENT_BEGINS must come immediately before '{restart}' ({i_mark} vs {i_restart})")
    check(i_after > i_restart, f"{key}: fleet guard must follow the restart")
    check("wait 60s, then 4 samples every 20s" in ev[i_after][1], f"{key}: fleet guard defaults changed: {ev[i_after][1]!r}")
    check(sum(1 for e in ev if e[0] == "PLAN" and e[1].startswith(restart)) == 1, f"{key}: exactly one restart step in apply")

cpf = [e for e in events("plan-o0_deploy_caddy-preflight.txt") if e[0] == "PLAN"]
i_cand = idx(cpf, lambda e: e[1].startswith("candidate resolves inside the stage dir"), "caddy preflight candidate-dir step")
i_bind = idx(cpf, lambda e: e[1].startswith("candidate and bundle snippet are exactly what the O0-A05P local probe passed"), "caddy preflight probe binding")
i_stage = idx(cpf, lambda e: e[1].startswith("stage the bundle's snippet next to the candidate"), "caddy preflight staging")
check(i_cand == 1 and i_bind < i_stage, f"caddy preflight: candidate-dir check right after the gate clear, probe binding before the first staging write ({i_cand},{i_bind},{i_stage})")
check("probe_candidate_sha256=" in cpf[-1][2] and "probe_snippet_sha256=" in cpf[-1][2], "caddy preflight gate must record the probe sha256 values")
capply = [e for e in events("plan-o0_deploy_caddy-apply.txt") if e[0] == "PLAN"]
check("probe_candidate_sha256=" in capply[0][2] and "probe_snippet_sha256=" in capply[0][2], "caddy apply must re-check the probe sha256 values in its REQUIRE")

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
for name, before_what in (("plan-o0_deploy_watcher_gateway-preflight.txt", "record the passed wgw-preflight gate"),
                          ("plan-o0_deploy_watcher_gateway-apply.txt", "fleet baseline (before-wgw)")):
    ev = events(name)
    i_iso = idx(ev, lambda e: e[1].startswith("control-plane unit isolation (S-10)"), f"{name} isolation gate")
    i_next = idx(ev, lambda e: e[1].startswith(before_what), f"{name} {before_what}")
    check(i_iso < i_next, f"{name}: the isolation gate must come before '{before_what}'")
    check("cp-isolation" in ev[i_iso][2] and "--other-unit trader-v3-controlplane-node-control" in ev[i_iso][2]
          and "--other-unit trader-v3-controlplane-event-ingest" in ev[i_iso][2] and "--wgw-unit trader-v3-controlplane-watcher-gateway.service" in ev[i_iso][2],
          f"{name}: isolation gate must check all four units")
# WGW-1.0.4 RS-16 order and red lines (review wac-096 r5 🟡-B, 🟡-C)
ev = [e for e in events("plan-o0_deploy_watcher_gateway-apply.txt") if e[0] == "PLAN"]
text = "".join(e[1] + "\n" + e[2] for e in ev)
i_dir = idx(ev, lambda e: e[1].startswith("install the release tree"), "wgw install dir")
i_smoke = idx(ev, lambda e: e[1].startswith("dependency check AS THE UNIT USER"), "wgw user smoke")
i_env = idx(ev, lambda e: e[1].startswith("install the watcher-gateway env"), "wgw env")
i_unit = idx(ev, lambda e: e[1].startswith("install the unit file"), "wgw unit")
i_rc = idx(ev, lambda e: e[1].startswith("NeedDaemonReload=no on EVERY unit (apply-before-enable"), "wgw reload check")
i_dr = idx(ev, lambda e: e[1].startswith("daemon-reload (the only one"), "wgw daemon-reload")
i_en = idx(ev, lambda e: e[1].startswith("enable watcher-gateway WITHOUT a reload"), "wgw enable")
i_ra = idx(ev, lambda e: e[1].startswith("NeedDaemonReload=no on EVERY unit (apply-after-daemon-reload"), "wgw reload check after")
i_st = idx(ev, lambda e: e[1] == "start watcher-gateway", "wgw start")
# review wac-108 🟡-1: enable --no-reload, then AT ONCE the one daemon-reload (it clears systemd >= 255's host-wide mark), a
# check that every unit is back to no, then start
check(i_dir < i_smoke < i_env < i_unit < i_rc < i_en and i_dr == i_en + 1 and i_dr < i_ra < i_st,
      f"wgw apply order ({i_dir},{i_smoke},{i_env},{i_unit},{i_rc},{i_en},{i_dr},{i_ra},{i_st})")
check("--as-user trader-v3-cp-watcher-gateway" in ev[i_smoke][2] and "--exclude trader-v3-controlplane-watcher-gateway.service" in ev[i_rc][2]
      and "--exclude" not in ev[i_ra][2], "wgw: smoke as the unit user; the pre-enable check excludes only the new unit, the post-reload check nothing")
check(sum(1 for e in ev if "systemctl daemon-reload" in e[2] or e[2].strip().endswith("systemctl daemon-reload")) == 1, "wgw apply: exactly one daemon-reload")
check("enable --now" not in text and "systemctl restart" not in text and "pip " not in text and "jp24-p1-control-plane" not in text,
      "wgw apply: no enable --now, no restart of anything, no pip, no whole-plane script")
check(not re.search(r"systemctl (stop|restart|reload|kill|start) [^\n]*(node-control|event-ingest|operator-query)", text), "wgw apply never acts on the three units")
rb = [e for e in events("plan-o0_deploy_watcher_gateway-rollback.txt") if e[0] == "PLAN"]
i_r = idx(rb, lambda e: e[1].startswith("record host-wide NeedDaemonReload BEFORE the disable"), "wgw rollback pre-disable listing")
i_w = idx(rb, lambda e: e[1].startswith("withdraw the exposure FIRST"), "wgw rollback withdraw")
i_n = idx(rb, lambda e: e[1].startswith("judge the listing taken BEFORE the disable"), "wgw rollback reload gate")
check(i_r < i_w < i_n and "disable --no-reload" in rb[i_w][2] and "daemon-reload-check" not in rb[i_w][2] + rb[i_r][2]
      and "reload-rollback-before-disable.txt" in rb[i_n][2] and "reload-rollback-after.txt" in rb[i_n][2],
      "wgw rollback: record the host-wide listing, then stop + disable --no-reload, then gate on the PRE-disable listing (and prove all no after)")

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
print("STRUCTURE_OK preflight_gate_clear=4 caddy_probe_binding=ok apply_order=3 restore_db_order=ok wgw_isolation_gate=2 wgw_order=ok wgw_rollback_order=ok build_graceful_stop=ok build_backup_api=ok")
PY

# ---------------------------------------------------------------- 2. automatic rollback (sandbox)
if [ "$STRUCTURE_ONLY" = 1 ]; then :
elif [ -e /srv/trader-v3 ]; then echo "SKIP sandbox part: this host has /srv/trader-v3"; else
BIN="$WORK/bin"; mkdir -p "$BIN"
printf '#!/usr/bin/env bash\n[ "$1" = "-u" ] && { echo 0; exit 0; }\ngrep -qx "$1" "$CALLS.users" 2>/dev/null && { echo "uid=999($1) gid=999($1)"; exit 0; }\n/usr/bin/id "$@"\n' > "$BIN/id"
# stage O (WGW-1.0.4): system users/groups in a stub db; setpriv runs the command (the unit-user smoke; SMOKE_AS_USER_MISSING
# injects the DEPENDENCY_MISSING the shared venv would give); ss lists 8186 only while the new unit runs
cat > "$BIN/getent" <<'STUB'
#!/usr/bin/env bash
grep -qx "$2" "$CALLS.users" 2>/dev/null && { echo "$2:x:999:999::/nonexistent:/usr/sbin/nologin"; exit 0; }
exit 2
STUB
for v in useradd groupadd; do printf '#!/usr/bin/env bash\necho "%s $*" >> "$CALLS"\nn="${*: -1}"; grep -qx "$n" "$CALLS.users" 2>/dev/null || echo "$n" >> "$CALLS.users"\n' "$v" > "$BIN/$v"; done
for v in userdel groupdel; do printf '#!/usr/bin/env bash\necho "%s $*" >> "$CALLS"\nn="${*: -1}"; [ -f "$CALLS.users" ] && grep -vx "$n" "$CALLS.users" > "$CALLS.users.t"; mv -f "$CALLS.users.t" "$CALLS.users" 2>/dev/null || true\n' "$v" > "$BIN/$v"; done
cat > "$BIN/setpriv" <<'STUB'
#!/usr/bin/env bash
# the smoke runs its child with a minimal environment: this stub signals through files next to itself
here="$(dirname "$0")"; echo "setpriv $1" >> "$here/setpriv.log"
while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do shift; done; shift
if [ -e "$here/SMOKE_AS_USER_MISSING" ]; then echo "DEPENDENCY_MISSING httpx"; exit 3; fi
exec "$@"
STUB
cat > "$BIN/ss" <<'STUB'
#!/usr/bin/env bash
echo 'LISTEN 0 4096 127.0.0.1:8183 0.0.0.0:* users:(("uvicorn",pid=11,fd=3))'
last="$(grep -E '^systemctl (start|stop) trader-v3-controlplane-watcher-gateway' "$CALLS" 2>/dev/null | tail -1)"
case "$last" in *" start "*) echo 'LISTEN 0 4096 127.0.0.1:8186 0.0.0.0:* users:(("uvicorn",pid=12,fd=3))' ;; esac
STUB
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
W=trader-v3-controlplane-watcher-gateway.service
WF="$O0_SANDBOX/etc/systemd/system/$W"
# review wac-108 🟡-1: systemd >= 255 (unit.c:3915, dbus-manager.c:2524) - an enable/disable that changes unit files sets the
# manager-level unit_file_state_outdated: EVERY unit reports NeedDaemonReload=yes until the next daemon-reload. SYSTEMD_VERSION
# (default 255) switches to the v254 behaviour (per-unit only).
OUTDATED="$CALLS.outdated"
case "$1 $2" in
  "enable --no-reload"|"disable --no-reload") [ "${SYSTEMD_VERSION:-255}" -lt 255 ] || : > "$OUTDATED" ;;
  "daemon-reload "*) rm -f "$OUTDATED" ;;
esac
case "$1" in list-units) printf 'caddy.service loaded active running Caddy\ntrader-v3-controlplane-operator-query.service loaded active running oq\nx.timer loaded active waiting x\n'; exit 0 ;; esac
if [ "$1" = show ] && [[ " $* " == *" -p Id -p NeedDaemonReload "* ]]; then
  for u in "$@"; do case "$u" in show|-p|Id|NeedDaemonReload) continue ;; esac
    v=no
    [ ! -e "$OUTDATED" ] || v=yes
    if [ "$u" = x.timer ] && { [ "${RELOAD_PENDING:-}" = always ] || { [ "${RELOAD_PENDING:-}" = after-start ] && grep -q "^systemctl start $W" "$CALLS"; }; }; then v=yes; fi
    printf 'Id=%s\nNeedDaemonReload=%s\n\n' "$u" "$v"; done
  exit 0
fi
if [ "$1" = show ] && [[ "$*" == *"MainPID,ExecMainStartTimestamp"* ]]; then printf '4242\nTue 2026-09-29 00:00:00 UTC\n'; exit 0; fi
if [ "$1" = show ] && [[ "$*" == *"-p LoadState --value $W"* ]]; then
  if [ -e "$WF" ] && grep -q '^systemctl daemon-reload' "$CALLS"; then echo loaded; else echo not-found; fi; exit 0
fi
if [ "$1" = show ] && [[ "$*" == *"-p ActiveState --value $W"* ]]; then
  last="$(grep -E "^systemctl (start|stop) $W" "$CALLS" | tail -1)"; case "$last" in *" start "*) echo active ;; *) echo inactive ;; esac; exit 0
fi
if [ "$1" = show ] && [ "$2" = "$W" ]; then
  if [ -e "$WF" ] && grep -q '^systemctl daemon-reload' "$CALLS"; then
    nd=no; [ ! -e "$OUTDATED" ] || nd=yes
    printf 'LoadState=loaded\nNeedDaemonReload=%s\nFragmentPath=%s\n' "$nd" "$WF"
    sed -n 's/^User=/User=/p; s/^WorkingDirectory=/WorkingDirectory=/p' "$WF"
    printf 'EnvironmentFiles=%s (ignore_errors=no)\n' "$(sed -n 's/^EnvironmentFile=//p' "$WF")"
    printf 'Environment=%s\n' "$(sed -n 's/^Environment=//p' "$WF" | tr '\n' ' ')"
    printf 'ExecStart={ path=x ; argv[]=%s ; }\n' "$(sed -n 's/^ExecStart=//p' "$WF")"
  else printf 'LoadState=not-found\nNeedDaemonReload=no\n'; fi
  exit 0
fi
if [ "$1" = start ] && [ -n "${START_FAIL:-}" ]; then exit 1; fi
# unit properties for the stage O isolation gate (o0_tool.py cp-isolation); ISOLATION_FAIL
# injects the two S-10 violations (env file / Environment= name) with a sentinel VALUE
if [ "$1" = "show" ] && [[ " $* " == *" -p LoadState "* ]]; then
  echo LoadState=loaded; echo "WorkingDirectory=$O0_SANDBOX/srv/trader-v3/services/control-plane/api"
  # wac-060 (review wac-072 🟡-3): reload = node-control drop-in changed on disk but not loaded;
  # unparsed = an EnvironmentFiles= value the gate cannot read
  if { [ "${ISOLATION_FAIL:-}" = reload ] && [[ "$2" == *node-control ]]; } || [ -e "$OUTDATED" ]; then echo NeedDaemonReload=yes; else echo NeedDaemonReload=no; fi
  if [ "${ISOLATION_FAIL:-}" = unparsed ] && [[ "$2" == *event-ingest ]]; then echo 'EnvironmentFiles=etc/relative.env (ignore_errors=no)'; fi
  oqe="$O0_SANDBOX/srv/trader-v3/secrets/control-plane/operator-query.env"
  case "$2" in
    *operator-query) echo "EnvironmentFiles=$oqe (ignore_errors=no)"; echo 'Environment=PYTHONPATH=/x CONTROL_PLANE_APP_ROLE=operator-query' ;;
    *node-control) [ "${ISOLATION_FAIL:-}" != envfile ] || echo "EnvironmentFiles=$oqe (ignore_errors=no)"; echo 'Environment=CONTROL_PLANE_APP_ROLE=node-control' ;;
    *event-ingest) if [ "${ISOLATION_FAIL:-}" = envname ]; then echo 'Environment=WATCHER_SNAPSHOT_TOKEN=SENTINELisoleak0123456789 CONTROL_PLANE_APP_ROLE=event-ingest'
                   else echo 'Environment=CONTROL_PLANE_APP_ROLE=event-ingest'; fi ;;
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
# wac-092: CADDY_REAL = the real Caddy v2.10.2 (O0_CADDY_BIN) for the real-Caddy preflight case; its state dirs go to
# a test directory, never the caller's HOME
if [ -n "${CADDY_REAL:-}" ]; then
  HOME="$CADDY_REAL_HOME" XDG_CONFIG_HOME="$CADDY_REAL_HOME/config" XDG_DATA_HOME="$CADDY_REAL_HOME/data" exec "$CADDY_REAL" "$@"
fi
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
if "-d" in args:
    for a in args[args.index("-d") + 1:]:
        if not a.startswith("-") and a not in (args[args.index(x) + 1] for x in ("-m", "-o", "-g") if x in args):
            os.makedirs(a, exist_ok=True)
    sys.exit(0)
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
if "-m" in args:
    os.chmod(dst, int(args[args.index("-m") + 1], 8))
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
argv = [a for a in sys.argv[1:] if a != "-R"]
owner = argv[0].replace("root", "0")
for f in argv[1:]:
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
  printf 'import caddy-watcher-gateway.caddy\n(watcher_gateway_upstream) {\n\turi strip_prefix /m\n\treverse_proxy 127.0.0.1:8186\n}\njp-bot.balen.wang {\n\timport watcher_gateway_routes\n\trespond 204\n}\n' > "$S/caddy/Caddyfile.candidate"
  cp "$S/bundle/caddy/caddy-watcher-gateway.caddy" "$S/caddy/caddy-watcher-gateway.caddy"   # preflight stages it (relative import)
  (cd "$SB/etc/caddy" && sha256sum "$SB/etc/caddy/Caddyfile" "$SB/etc/caddy/v3.env") > "$S/evidence/caddy-live.sha256"
  seal_bundle
  python3 "$O0/o0_tool.py" gate-write --out "$S/evidence/caddy-preflight.gate.json" --stage caddy-preflight --bundle "$S/bundle" \
    --file-sha "candidate_caddyfile=$S/caddy/Caddyfile.candidate" --file-sha "live_caddyfile=$SB/etc/caddy/Caddyfile" \
    --file-sha "live_caddy_env=$SB/etc/caddy/v3.env" --file-sha "cred_caddy_env=$S/creds/set-initial/caddy.env" \
    --file-sha "staged_snippet=$S/caddy/caddy-watcher-gateway.caddy" \
    --file-sha "probe_candidate_sha256=$S/caddy/Caddyfile.candidate" --file-sha "probe_snippet_sha256=$S/bundle/caddy/caddy-watcher-gateway.caddy" >/dev/null
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
# SN4 (wac-092): a DANGLING symlink sits at the live snippet path (installing would write through it): refused before any write
caddy_setup SN4
ln -s "$SB/etc/caddy/nowhere.caddy" "$SB/etc/caddy/caddy-watcher-gateway.caddy"
run_apply o0_deploy_caddy.sh O0-A05
if [ "$RC" -ne 0 ] && [ ! -e "$S/backup-caddy" ] && ! grep -q 'systemctl restart' "$CALLS" && [ -L "$SB/etc/caddy/caddy-watcher-gateway.caddy" ] \
   && [ ! -e "$SB/etc/caddy/nowhere.caddy" ] && sha256sum -c --quiet "$S/evidence/caddy-live.sha256" >/dev/null 2>&1 && ! printf '%s' "$OUT" | grep -q AUTO_ROLLBACK_ARMED; then
  ok "caddy SN4 (a dangling symlink at the live snippet path): refused at the snippet gate, nothing backed up, written through or restarted"
else bad "caddy SN4: rc=$RC"; printf '%s\n' "$OUT" | grep -E 'STEP|FAIL' | tail -5 | sed 's/^/    /'; fi
# PB (wac-090, review wac-088 🟡-4): the preflight gate carries a probe sha that is not the candidate in hand
caddy_setup PB
printf 'probed but different\n' > "$WORK/other-candidate"
python3 "$O0/o0_tool.py" gate-write --out "$S/evidence/caddy-preflight.gate.json" --stage caddy-preflight --bundle "$S/bundle" \
  --file-sha "candidate_caddyfile=$S/caddy/Caddyfile.candidate" --file-sha "live_caddyfile=$SB/etc/caddy/Caddyfile" \
  --file-sha "live_caddy_env=$SB/etc/caddy/v3.env" --file-sha "cred_caddy_env=$S/creds/set-initial/caddy.env" \
  --file-sha "staged_snippet=$S/caddy/caddy-watcher-gateway.caddy" \
  --file-sha "probe_candidate_sha256=$WORK/other-candidate" --file-sha "probe_snippet_sha256=$S/bundle/caddy/caddy-watcher-gateway.caddy" >/dev/null
run_apply o0_deploy_caddy.sh O0-A05
if [ "$RC" -ne 0 ] && [ ! -e "$S/backup-caddy" ] && ! grep -q 'systemctl restart' "$CALLS" && [ ! -e "$SB/etc/caddy/caddy-watcher-gateway.caddy" ] \
   && printf '%s' "$OUT" | grep -q 'gate field probe_candidate_sha256 differs' && sha256sum -c --quiet "$S/evidence/caddy-live.sha256" >/dev/null 2>&1; then
  ok "caddy PB (gate's probe sha is not the candidate's): refused at the gate, nothing backed up, written or restarted"
else bad "caddy PB: rc=$RC"; printf '%s\n' "$OUT" | grep -E 'STEP|FAIL' | tail -5 | sed 's/^/    /'; fi

# ---- Caddy PREFLIGHT executed in the sandbox (wac-092, review wac-090 🟡-5): until now the preflight was only
# checked as plan output, so the probe-sha binding (R13), the symlink checks (R16) and the live-snippet comparison
# (G29) had no executed test. Each case: what must be refused is refused BEFORE the staged snippet is written, no
# gate file appears, the live Caddy directory is byte-for-byte unchanged, nothing is restarted.
pf_setup() {  # pf_setup <name>: live files, a candidate in $S/caddy, a fake control-plane catalog; no gate yet
  new_sandbox "caddy-pf-$1"
  mkdir -p "$SB/etc/caddy" "$S/caddy"
  printf 'old.example {\n\trespond 200\n}\n' > "$SB/etc/caddy/Caddyfile"
  printf 'CADDY_DOMAIN=jp-bot.balen.wang\n' > "$SB/etc/caddy/v3.env"
  printf 'import caddy-watcher-gateway.caddy\n(watcher_gateway_upstream) {\n\turi strip_prefix /m\n\treverse_proxy 127.0.0.1:8186\n}\njp-bot.balen.wang {\n\timport watcher_gateway_routes\n\trespond 204\n}\n' > "$S/caddy/Caddyfile.candidate"
  PF_CAT="$WORK/catalog-pf-$1.env"; fake_catalog "$PF_CAT"
  seal_bundle
  PF_CAND="$(sha256sum "$S/caddy/Caddyfile.candidate" | cut -d' ' -f1)"; PF_SNIP="$(sha256sum "$S/bundle/caddy/caddy-watcher-gateway.caddy" | cut -d' ' -f1)"
  PF_CAND_ARG="$PF_CAND"; PF_SNIP_ARG="$PF_SNIP"
}
pf_live_snapshot() { (cd "$SB/etc/caddy" && find . -print | sort && find . -type f -print0 | sort -z | xargs -0 sha256sum) > "$WORK/pf-live-$1.txt"; }
pf_live_unchanged() { (cd "$SB/etc/caddy" && find . -print | sort && find . -type f -print0 | sort -z | xargs -0 sha256sum) | cmp -s - "$WORK/pf-live-$1.txt"; }
run_pf() {  # run_pf: the real preflight phase with --execute in the sandbox -> RC, OUT
  RC=0
  OUT="$(PATH="$BIN:$PATH" O0_SANDBOX="$SB" O0_FLEET_PARAMS_SANDBOX_SKIP=1 CALLS="$CALLS" O0_FLEET_NODES=account-a O0_FLEET_READY_PORTS=8081 \
         bash "$O0/o0_deploy_caddy.sh" --execute --phase preflight --auth-id O0-A05 --stage-dir "$S" --catalog-env "$PF_CAT" \
         --probe-candidate-sha256 "$PF_CAND_ARG" --probe-snippet-sha256 "$PF_SNIP_ARG" 2>&1)" || RC=$?
}
pf_refused() {  # pf_refused <label> <expected output ERE> <live-snapshot name>
  if [ "$RC" -ne 0 ] && printf '%s' "$OUT" | grep -Eq "$2" && [ ! -e "$S/evidence/caddy-preflight.gate.json" ] \
     && [ ! -e "$S/caddy/caddy-watcher-gateway.caddy" ] && [ ! -L "$S/caddy/caddy-watcher-gateway.caddy" ] && pf_live_unchanged "$3" \
     && ! grep -qE 'systemctl (restart|reload)|caddy (validate|adapt)' "$CALLS"; then
    ok "$1: refused before the staged snippet is written; no gate, live Caddy dir unchanged, no validate/adapt/restart"
  else bad "$1: rc=$RC staged=$([ -e "$S/caddy/caddy-watcher-gateway.caddy" ] && echo yes || echo no) gate=$([ -e "$S/evidence/caddy-preflight.gate.json" ] && echo yes || echo no)"
       printf '%s\n' "$OUT" | grep -E 'STEP|FAIL|ABORT|BINDING|symlink|outside' | tail -5 | sed 's/^/    /'; fi
}
# PF1: everything in order: every marker, the gate records both probe sha256 values; then apply ACCEPTS that gate
pf_setup PF1; pf_live_snapshot PF1; run_pf
gate="$S/evidence/caddy-preflight.gate.json"
pf_markers() { local m; for m in "$@"; do printf '%s' "$OUT" | grep -q -- "$m" || { echo "    missing marker: $m"; return 1; }; done; }
if [ "$RC" -eq 0 ] && pf_markers CANDIDATE_IN_STAGING FLEET_BASELINE_OK PROBE_BINDING_OK LIVE_SNIPPET_ABSENT SNIPPET_STAGED CADDYFILE_CHECK_OK \
     'CADDY_WATCHER_ROUTES_OK mode=snippet lines=16' 'GATE_WRITTEN stage=caddy-preflight' \
   && python3 -c 'import json,sys; f=json.load(open(sys.argv[1]))["fields"]; sys.exit(0 if f["probe_candidate_sha256"]==sys.argv[2] and f["probe_snippet_sha256"]==sys.argv[3] else 1)' "$gate" "$PF_CAND" "$PF_SNIP" \
   && cmp -s "$S/caddy/caddy-watcher-gateway.caddy" "$S/bundle/caddy/caddy-watcher-gateway.caddy" && pf_live_unchanged PF1 && ! grep -q 'systemctl restart' "$CALLS"; then
  ok "caddy PF1 preflight (sandbox, executed): all markers, gate records probe_candidate/snippet_sha256, staged snippet = bundle, live dir unchanged, no restart"
else bad "caddy PF1 preflight: rc=$RC"; printf '%s\n' "$OUT" | grep -E 'STEP|FAIL|ABORT' | tail -6 | sed 's/^/    /'; fi
: > "$CALLS"; rm -f "$CALLS.active"
run_apply o0_deploy_caddy.sh O0-A05
if [ "$RC" -eq 0 ] && printf '%s' "$OUT" | grep -q 'FLEET_UNCHANGED_ALL_SAMPLES' && [ "$(grep -c 'systemctl restart caddy' "$CALLS")" = 1 ] \
   && cmp -s "$SB/etc/caddy/Caddyfile" "$S/caddy/Caddyfile.candidate" && cmp -s "$SB/etc/caddy/caddy-watcher-gateway.caddy" "$S/bundle/caddy/caddy-watcher-gateway.caddy"; then
  ok "caddy PF1 -> apply: the executed preflight's gate is accepted; installed files = candidate and bundle snippet; one restart; fleet unchanged"
else bad "caddy PF1 -> apply: rc=$RC"; printf '%s\n' "$OUT" | grep -E 'STEP|FAIL|ABORT' | tail -6 | sed 's/^/    /'; fi
# PF2 (R13): the bundle snippet is not the probed one (a well-formed but different sha256)
pf_setup PF2; pf_live_snapshot PF2; PF_SNIP_ARG="$PF_CAND"; run_pf
pf_refused "caddy PF2 (R13, snippet sha is not the probed one)" 'PROBE_BINDING_FAILED bundle snippet sha256' PF2
# PF3: the candidate is not the probed one
pf_setup PF3; pf_live_snapshot PF3; PF_CAND_ARG="$PF_SNIP"; run_pf
pf_refused "caddy PF3 (candidate sha is not the probed one)" 'PROBE_BINDING_FAILED candidate sha256' PF3
# PF4 (R16): the candidate path is a symlink (to a file inside the stage dir)
pf_setup PF4; mv "$S/caddy/Caddyfile.candidate" "$S/caddy/real.candidate"; ln -s real.candidate "$S/caddy/Caddyfile.candidate"; pf_live_snapshot PF4; run_pf
pf_refused "caddy PF4 (R16, candidate is a symlink)" 'candidate or staged snippet path is a symlink' PF4
# PF5 (R16): the staged snippet path is a dangling symlink INTO the live Caddy directory (install would write through it)
pf_setup PF5; ln -s "$SB/etc/caddy/caddy-watcher-gateway.caddy" "$S/caddy/caddy-watcher-gateway.caddy"; pf_live_snapshot PF5; run_pf
if [ "$RC" -ne 0 ] && printf '%s' "$OUT" | grep -q 'candidate or staged snippet path is a symlink' && [ ! -e "$SB/etc/caddy/caddy-watcher-gateway.caddy" ] \
   && [ ! -e "$S/evidence/caddy-preflight.gate.json" ] && pf_live_unchanged PF5; then
  ok "caddy PF5 (R16, staged snippet path is a symlink into /etc/caddy): refused, nothing written through it, no gate"
else bad "caddy PF5: rc=$RC live_snippet=$([ -e "$SB/etc/caddy/caddy-watcher-gateway.caddy" ] && echo WRITTEN || echo absent)"; fi
# PF6 (G29): a DIFFERENT snippet file is already live
pf_setup PF6; printf '(watcher_gateway_routes) {\n}\n' > "$SB/etc/caddy/caddy-watcher-gateway.caddy"; pf_live_snapshot PF6; run_pf
pf_refused "caddy PF6 (G29, a different live snippet file)" 'LIVE_SNIPPET_DIFFERS' PF6
# PF9 (wac-092): a dangling symlink at the live snippet path is not "absent"
pf_setup PF9; ln -s "$SB/etc/caddy/nowhere.caddy" "$SB/etc/caddy/caddy-watcher-gateway.caddy"; pf_live_snapshot PF9; run_pf
pf_refused "caddy PF9 (dangling symlink at the live snippet path)" 'LIVE_SNIPPET_DIFFERS' PF9
# PF7: the same snippet is already live (re-run after a rollback that kept it): accepted
pf_setup PF7; cp "$S/bundle/caddy/caddy-watcher-gateway.caddy" "$SB/etc/caddy/caddy-watcher-gateway.caddy"; pf_live_snapshot PF7; run_pf
if [ "$RC" -eq 0 ] && printf '%s' "$OUT" | grep -q LIVE_SNIPPET_EQUALS_BUNDLE && [ -e "$S/evidence/caddy-preflight.gate.json" ] && pf_live_unchanged PF7; then
  ok "caddy PF7 (identical live snippet): LIVE_SNIPPET_EQUALS_BUNDLE, gate written, live dir unchanged"
else bad "caddy PF7: rc=$RC"; printf '%s\n' "$OUT" | grep -E 'STEP|FAIL|ABORT' | tail -4 | sed 's/^/    /'; fi
# PF8 (R16, the pwd -P layer): $S/caddy is a symlink to the live Caddy directory
pf_setup PF8; mv "$S/caddy/Caddyfile.candidate" "$SB/etc/caddy/Caddyfile.candidate"; rmdir "$S/caddy"; ln -s "$SB/etc/caddy" "$S/caddy"
pf_live_snapshot PF8; run_pf
if [ "$RC" -ne 0 ] && printf '%s' "$OUT" | grep -q 'is outside the stage dir' && [ ! -e "$SB/etc/caddy/caddy-watcher-gateway.caddy" ] \
   && [ ! -e "$S/evidence/caddy-preflight.gate.json" ] && pf_live_unchanged PF8; then
  ok "caddy PF8 (stage caddy/ dir is a symlink to /etc/caddy): refused by the resolved-path check, nothing written in the live dir"
else bad "caddy PF8: rc=$RC"; printf '%s\n' "$OUT" | grep -E 'STEP|FAIL|ABORT|outside' | tail -4 | sed 's/^/    /'; fi
# PF-REAL (only with O0_CADDY_BIN): the same preflight with the REAL Caddy v2.10.2 doing validate and adapt of a
# production-shaped candidate (tests/fixtures/caddy/Caddyfile.prodlike.in, fresh one-off bcrypt): the gate is
# written; a candidate with the review wac-090 n08 injection (`request_header /m//v1/...`) is refused by verify.
if [ -n "${O0_CADDY_BIN:-}" ]; then
  export CADDY_REAL_HOME="$WORK/caddy-real-home"; mkdir -p "$CADDY_REAL_HOME"
  pf_hash="$("$O0_CADDY_BIN" hash-password --plaintext "$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')")"
  for v in good n08; do
    pf_setup "REAL-$v"
    sed "s|@@BCRYPT@@|$pf_hash|" "$HERE/fixtures/caddy/Caddyfile.prodlike.in" > "$S/caddy/Caddyfile.candidate"
    [ "$v" = good ] || python3 - "$S/caddy/Caddyfile.candidate" <<'PY'
import sys
p = sys.argv[1]; t = open(p).read(); imp = "\timport watcher_gateway_routes\n"
open(p, "w").write(t.replace(imp, imp + '\trequest_header /m//v1/watcher/trading/accounts Authorization "Bearer {env.SYSTEM_OBSERVER_TOKEN}"\n', 1))
PY
    PF_CAND="$(sha256sum "$S/caddy/Caddyfile.candidate" | cut -d' ' -f1)"; PF_CAND_ARG="$PF_CAND"
    pf_live_snapshot "REAL-$v"
    CADDY_REAL="$O0_CADDY_BIN" run_pf
    if [ "$v" = good ] && [ "$RC" -eq 0 ] && printf '%s' "$OUT" | grep -q 'Valid configuration' && printf '%s' "$OUT" | grep -q 'CADDY_WATCHER_ROUTES_OK mode=snippet lines=16' \
       && [ -e "$S/evidence/caddy-preflight.gate.json" ] && pf_live_unchanged "REAL-$v" && ! printf '%s' "$OUT" | grep -q '\$2a\$'; then
      ok "caddy PF-REAL good: real Caddy validate + adapt, verify OK, gate written, live dir unchanged, no bcrypt hash in the output"
    elif [ "$v" = n08 ] && [ "$RC" -ne 0 ] && printf '%s' "$OUT" | grep -q "FAIL shadow .*path=/m//v1/watcher/trading/accounts" \
       && [ ! -e "$S/evidence/caddy-preflight.gate.json" ] && pf_live_unchanged "REAL-$v" && ! printf '%s' "$OUT" | grep -q '\$2a\$'; then
      ok "caddy PF-REAL n08: real Caddy adapt, verify refuses the '//' injection, no gate, live dir unchanged"
    else bad "caddy PF-REAL $v: rc=$RC"; printf '%s\n' "$OUT" | grep -E 'STEP|FAIL|ABORT' | tail -5 | sed 's/^/    /'; fi
  done
  unset CADDY_REAL_HOME
else echo "INFO caddy PF-REAL skipped (O0_CADDY_BIN unset)"; fi

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

# ---- stage O (WGW-1.0.4 §9.14.6, RS-16; task wac-105): watcher-gateway, own unit / dir / env; the three running units never touched
wgw_setup() {  # wgw_setup <name> [missing]  -> a sandbox with a fake shared tree, a fake release tree, the bundle and a cred set
  new_sandbox "wgw-$1"
  TR="$SB/srv/trader-v3"; RSHA="$(printf '%040d' 7)"
  mkdir -p "$TR/services/control-plane/api" "$TR/packages/execution-domain" "$TR/secrets/control-plane" "$TR/.venv-cp/bin" "$SB/etc/systemd/system" \
           "$S/bundle/watcher-gateway" "$WORK/rel-$1/services/control-plane/api"
  ln -s "$(command -v python3)" "$TR/.venv-cp/bin/python"
  echo 'shared = 1' > "$TR/services/control-plane/api/read_api.py"; echo 'x = 1' > "$TR/packages/execution-domain/a.py"
  printf 'services/control-plane/api/read_api.py\npackages/execution-domain/a.py\n' > "$WORK/shared-$1.paths"
  python3 "$O0/o0_tool.py" manifest-build --root "$TR" --paths-file "$WORK/shared-$1.paths" --out "$S/bundle/cp-shared.baseline.sha256" >/dev/null
  python3 -c 'import secrets,sys; open(sys.argv[1],"w").write("".join("%s=%s\n" % (n, secrets.token_urlsafe(32)) for n in ("RISK_ADMIN_TOKEN","VIEWER_TOKEN","REVIEWER_TOKEN","SYSTEM_OBSERVER_TOKEN")))' \
    "$TR/secrets/control-plane/operator-query.env"
  R="$WORK/rel-$1/services/control-plane/api"
  { [ "${2:-}" = missing ] && echo 'import o0_no_such_module_stand_in'
    printf 'class _R:\n    def __init__(s, n, p): s.name, s.path = n, p\nclass _A:\n    routes = [_R("watcher_gateway__status", "/v1/watcher/status"), _R("role_database_health", "/health/role")]\n'
    printf 'def create_app(role=None):\n    return _A()\napp = create_app()\n'; } > "$R/read_api.py"
  printf 'import os\ndef readiness():\n    return (bool(os.environ.get("WATCHER_GATEWAY_TOKEN")), "ok")\n' > "$R/watcher_gateway.py"
  (cd "$WORK/rel-$1" && tar -cf "$S/bundle/watcher-gateway/release.tar" services && find services -type f | sort > "$WORK/rel-$1.paths")
  python3 "$O0/o0_tool.py" manifest-build --root "$WORK/rel-$1" --paths-file "$WORK/rel-$1.paths" --out "$S/bundle/watcher-gateway/release.sha256" >/dev/null
  python3 "$O0/o0_tool.py" wgw-unit --render --trader-root "$TR" --release-sha "$RSHA" --resource-conf "$REPO/infra/systemd/account-stall-control-plane-reader.conf" \
    --out "$S/bundle/watcher-gateway/trader-v3-controlplane-watcher-gateway.service" >/dev/null
  printf 'WATCHER_GATEWAY_TOKEN\nRISK_ADMIN_TOKEN\nVIEWER_TOKEN\nREVIEWER_TOKEN\nSYSTEM_OBSERVER_TOKEN\n' > "$S/bundle/watcher-gateway/env.whitelist"
  seal_bundle
}
run_wgw() {  # run_wgw <phase> args...  -> RC, OUT
  local phase="$1"; shift
  RC=0
  OUT="$(PATH="$BIN:$PATH" O0_SANDBOX="$SB" O0_FLEET_PARAMS_SANDBOX_SKIP=1 CALLS="$CALLS" O0_FLEET_NODES=account-a O0_FLEET_READY_PORTS=8081 O0_FLEET_SETTLE_S=0 \
         O0_FLEET_SAMPLES=1 O0_FLEET_INTERVAL_S=0 O0_WGW_HEALTH_TRIES=1 bash "$O0/o0_deploy_watcher_gateway.sh" --execute --phase "$phase" --auth-id O0-A08 \
         --stage-dir "$S" "$@" 2>&1)" || RC=$?
}
wgw_nothing_new() {  # wgw_nothing_new <label> [objects-only]: no user, dir, env, unit file; (unless objects-only) no start/enable/daemon-reload
  local acts='^systemctl (start|enable|daemon-reload)'; [ "${2:-}" = objects-only ] && acts='^o0-never-matches$'
  if [ ! -e "$TR/releases/watcher-gateway/$RSHA" ] && [ ! -e "$TR/secrets/control-plane/watcher-gateway.env" ] && [ ! -e "$SB/etc/systemd/system/trader-v3-controlplane-watcher-gateway.service" ] \
     && ! grep -qx trader-v3-cp-watcher-gateway "$CALLS.users" 2>/dev/null && ! grep -qE "$acts" "$CALLS" \
     && cmp -s "$TR/services/control-plane/api/read_api.py" <(echo 'shared = 1'); then ok "$1: no new object left, nothing started or reloaded, shared dir intact"
  else bad "$1: leftovers or actions"; grep -E '^systemctl (start|enable|daemon-reload|stop|disable)|useradd|userdel' "$CALLS" | sed 's/^/    /'; fi
}
# PF-OK: the whole preflight runs in the sandbox and writes the gate (every other apply case starts from such a gate)
wgw_setup PFOK
run_wgw preflight
if [ "$RC" = 0 ] && [ -f "$S/evidence/wgw-preflight.gate.json" ] && printf '%s' "$OUT" | grep -q 'MANIFEST_OK cp-shared-vs-67b401a' \
   && printf '%s' "$OUT" | grep -q 'DAEMON_RELOAD_CLEAN' && printf '%s' "$OUT" | grep -q 'SMOKE_OK as=current user' && printf '%s' "$OUT" | grep -q 'WGW_UNIT_LINT_OK' \
   && printf '%s' "$OUT" | grep -q 'WGW_ENV_WRITTEN names=WATCHER_GATEWAY_TOKEN,RISK_ADMIN_TOKEN,VIEWER_TOKEN,REVIEWER_TOKEN,SYSTEM_OBSERVER_TOKEN' \
   && printf '%s' "$OUT" | grep -q 'CP_ISOLATION_OK units=3 watcher_gateway=absent' && printf '%s' "$OUT" | grep -q 'PORT_FREE 8186' \
   && [ ! -e "$TR/releases" ] && ! grep -qE '^systemctl (start|enable|daemon-reload|stop|restart)' "$CALLS" && [ ! -e "$TR/services/control-plane/api/__pycache__" ]; then
  ok "wgw PF-OK: preflight passes in the sandbox (manifest, NeedDaemonReload, 8186 free, isolation, env names, python -B smoke, unit lint) and writes only the stage dir"
else bad "wgw PF-OK rc=$RC"; printf '%s\n' "$OUT" | grep -E 'STEP|FAIL|ABORT|SMOKE|DRIFT' | tail -8 | sed 's/^/    /'; fi
printf '%s' "$OUT" | grep -qE 'o0fake|SENTINEL|[A-Za-z0-9_-]{43}' && bad "wgw PF-OK: an env value may have been printed" || ok "wgw PF-OK: no token value in the output (names only)"
# PF-DRIFT (task wac-105): the shared dir differs from 67b401a -> stop with the file list and the options, nothing changed; the
# user's acceptance is bound to exactly that list
wgw_setup PFDRIFT
echo 'hotfix = 1' > "$TR/services/control-plane/api/read_api.py"
run_wgw preflight
dsha="$(printf '%s' "$OUT" | sed -n 's/.*MANIFEST_DRIFT cp-shared-vs-67b401a .*drift_sha256=\([0-9a-f]\{64\}\).*/\1/p' | head -1)"
if [ "$RC" != 0 ] && [ -n "$dsha" ] && printf '%s' "$OUT" | grep -q 'DRIFT MODIFIED services/control-plane/api/read_api.py' && printf '%s' "$OUT" | grep -q 'SUGGESTION option A' \
   && printf '%s' "$OUT" | grep -q 'NEVER here: fix, restore, overwrite' && [ "$(cat "$TR/services/control-plane/api/read_api.py")" = 'hotfix = 1' ] && [ ! -f "$S/evidence/wgw-preflight.gate.json" ]; then
  ok "wgw PF-DRIFT: preflight stops with the drift list, the user's options and drift_sha256; the drifted file is untouched, no gate"
else bad "wgw PF-DRIFT rc=$RC"; printf '%s\n' "$OUT" | grep -E 'DRIFT|SUGGESTION|STEP' | tail -6 | sed 's/^/    /'; fi
run_wgw preflight --accept-shared-drift "$(printf '%064d' 1)"
[ "$RC" != 0 ] && [ ! -f "$S/evidence/wgw-preflight.gate.json" ] && ok "wgw PF-DRIFT: an acceptance for another list is refused" || bad "wgw PF-DRIFT: foreign acceptance passed"
run_wgw preflight --accept-shared-drift "$dsha"
[ "$RC" = 0 ] && printf '%s' "$OUT" | grep -q MANIFEST_DRIFT_ACCEPTED && [ -f "$S/evidence/wgw-preflight.gate.json" ] \
  && ok "wgw PF-DRIFT: with the user's acceptance of exactly that list the preflight passes (before/after identity stays a gate)" || bad "wgw PF-DRIFT accepted rc=$RC"
# PF-MISSING (review wac-096 r5 🟡-C): the shared venv lacks a module -> DEPENDENCY_MISSING, stop, no gate, nothing pip-installed
wgw_setup PFMISS missing
run_wgw preflight
if [ "$RC" != 0 ] && printf '%s' "$OUT" | grep -q 'DEPENDENCY_MISSING o0_no_such_module_stand_in' && [ ! -f "$S/evidence/wgw-preflight.gate.json" ] \
   && ! grep -q pip "$CALLS"; then ok "wgw PF-MISSING: DEPENDENCY_MISSING stops the preflight (no gate, no pip)"
else bad "wgw PF-MISSING rc=$RC"; printf '%s\n' "$OUT" | grep -E 'SMOKE|DEPENDENCY|STEP' | tail -4 | sed 's/^/    /'; fi
# WA: the smoke AS THE UNIT USER fails (after the dir install) -> automatic rollback of the new objects, nothing started or reloaded
wgw_setup WA; run_wgw preflight; : > "$CALLS"
: > "$BIN/SMOKE_AS_USER_MISSING"; run_wgw apply; rm -f "$BIN/SMOKE_AS_USER_MISSING"
assert_rollback "wgw WA (unit-user smoke: DEPENDENCY_MISSING)" no '^systemctl (start|enable)' 0 "dependency check AS THE UNIT USER"
wgw_nothing_new "wgw WA"
# WB/WC run under both systemd behaviours (review wac-108 🟡-1): v254 (NeedDaemonReload per unit) and v255+ (an enable/disable
# marks EVERY unit yes until the next daemon-reload). The rollback gate judges the listing taken BEFORE its own disable.
for SV in 254 255; do
export SYSTEMD_VERSION=$SV
# WB: health fails after the start -> exposure withdrawn (stop, disable --no-reload), env and dir removed, unit file removed after a
# clean NeedDaemonReload check and ONE more daemon-reload, user removed
wgw_setup "WB$SV"; run_wgw preflight; : > "$CALLS"
run_wgw apply
assert_rollback "wgw WB v$SV (health fails after start)" yes '^systemctl start trader-v3-controlplane-watcher-gateway' 1 "watcher-gateway /health/role"
if grep -q '^systemctl enable --no-reload trader-v3-controlplane-watcher-gateway' "$CALLS" && grep -q '^systemctl stop trader-v3-controlplane-watcher-gateway' "$CALLS" \
   && grep -q '^systemctl disable --no-reload trader-v3-controlplane-watcher-gateway' "$CALLS" && [ "$(grep -c '^systemctl daemon-reload' "$CALLS")" = 2 ] \
   && ! grep -q 'enable --now\|disable --now' "$CALLS" && [ "$(grep -n '^systemctl stop trader-v3' "$CALLS" | cut -d: -f1)" -lt "$(grep -n '^systemctl daemon-reload' "$CALLS" | tail -1 | cut -d: -f1)" ]; then
  ok "wgw WB v$SV: enable --no-reload + start; rollback stops and disables (--no-reload) BEFORE its single daemon-reload"
else bad "wgw WB v$SV: systemctl sequence"; grep '^systemctl \(start\|stop\|enable\|disable\|daemon\)' "$CALLS" | sed 's/^/    /'; fi
wgw_nothing_new "wgw WB v$SV (after rollback)" objects-only
grep -q 'setpriv --reuid=trader-v3-cp-watcher-gateway' "$BIN/setpriv.log" && ok "wgw WB v$SV: the apply smoke ran as trader-v3-cp-watcher-gateway (setpriv)" || bad "wgw WB v$SV: no setpriv run"
# WC (review wac-096 r5 🟡-B): another unit gets NeedDaemonReload=yes after the start: the exposure is withdrawn anyway; the stopped,
# disabled unit file stays, no second daemon-reload, DAEMON_RELOAD_PENDING reported
wgw_setup "WC$SV"; run_wgw preflight; : > "$CALLS"
RELOAD_PENDING=after-start run_wgw apply
if [ "$RC" != 0 ] && grep -q '^systemctl stop trader-v3-controlplane-watcher-gateway' "$CALLS" && grep -q '^systemctl disable --no-reload trader-v3-controlplane-watcher-gateway' "$CALLS" \
   && [ "$(grep -c '^systemctl daemon-reload' "$CALLS")" = 1 ] && [ -e "$SB/etc/systemd/system/trader-v3-controlplane-watcher-gateway.service" ] \
   && [ ! -e "$TR/secrets/control-plane/watcher-gateway.env" ] && [ ! -e "$TR/releases/watcher-gateway/$RSHA" ] && printf '%s' "$OUT" | grep -q 'DAEMON_RELOAD_PENDING' \
   && grep -qE 'rollback_rc=[1-9]' "$S/evidence/auto-rollback.log"; then
  ok "wgw WC v$SV: NeedDaemonReload pending in the rollback: stopped + disabled + env/dir removed anyway; unit file kept, no second reload, DAEMON_RELOAD_PENDING"
else bad "wgw WC v$SV rc=$RC"; grep '^systemctl \(start\|stop\|enable\|disable\|daemon\)' "$CALLS" | sed 's/^/    /'; cat "$S/evidence/auto-rollback.log" 2>/dev/null; fi
done
unset SYSTEMD_VERSION
# WS (review wac-108 🟡-1): the SUCCESS path against fake services - apply (enable --no-reload, then at once the one
# daemon-reload, then start), verify (four-unit isolation must PASS: the host-wide mark is cleared), then a manual rollback
# (its gate uses the listing taken before its own disable) - under v254 and v255
python3 - "$WORK/fake-ports" <<'PY' > /dev/null 2>&1 &
import http.server, json, sys, threading
def srv(handler):
    s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s.server_address[1]
def mk(route):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            code, body = route(self.path, self.headers)
            data = json.dumps(body).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data)
        def log_message(self, *a):
            pass
    return H
def wgw(path, h):
    if path == "/health/role":
        return 200, {"status": "healthy", "app_role": "watcher-gateway", "database": "none", "gateway": "enabled"}
    if path == "/v1/watcher/status":
        return (200, {"ok": True}) if h.get("Authorization") else (401, {"code": "unauthenticated"})
    return 404, {"detail": "Not Found"}
def oq(path, h):
    return 404, {"detail": "Not Found"}
def v5(path, h):
    return (401, {}) if path in ("/m/v1/watcher/status", "/m/v1/accounts") else (404, {})
ports = [srv(mk(f)) for f in (wgw, oq, v5)]
open(sys.argv[1], "w").write(" ".join(map(str, ports)) + "\n")
threading.Event().wait()
PY
FAKE_PID=$!
for _ in $(seq 50); do [ -s "$WORK/fake-ports" ] && break; sleep 0.1; done
read -r P_WGW P_OQ P_V5 < "$WORK/fake-ports" || true
for SV in 254 255; do
  export SYSTEMD_VERSION=$SV O0_WGW_TEST_URL="http://127.0.0.1:$P_WGW" O0_OQ_TEST_URL="http://127.0.0.1:$P_OQ" O0_V5_TEST_PORT="$P_V5"
  wgw_setup "WS$SV"; run_wgw preflight; : > "$CALLS"
  run_wgw apply
  seq_ok=no
  en=$(grep -n '^systemctl enable --no-reload trader-v3-controlplane-watcher-gateway' "$CALLS" | cut -d: -f1); dr=$(grep -n '^systemctl daemon-reload' "$CALLS" | head -1 | cut -d: -f1)
  st=$(grep -n '^systemctl start trader-v3-controlplane-watcher-gateway' "$CALLS" | cut -d: -f1)
  [ -n "$en" ] && [ -n "$dr" ] && [ -n "$st" ] && [ "$en" -lt "$dr" ] && [ "$dr" -lt "$st" ] && [ "$(grep -c '^systemctl daemon-reload' "$CALLS")" = 1 ] && seq_ok=yes
  if [ "$RC" = 0 ] && [ "$seq_ok" = yes ] && printf '%s' "$OUT" | grep -q 'WGW_APPLY_DONE' && printf '%s' "$OUT" | grep -q 'PORT_LOOPBACK_ONLY 8186' \
     && grep -q 'NeedDaemonReload=no' "$S/evidence/reload-apply-after-daemon-reload.txt" && ! grep -q 'NeedDaemonReload=yes' "$S/evidence/reload-apply-after-daemon-reload.txt" \
     && [ "$(stat -f %Lp "$TR/secrets/control-plane/watcher-gateway.env" 2>/dev/null || stat -c %a "$TR/secrets/control-plane/watcher-gateway.env")" = 600 ]; then
    ok "wgw WS v$SV: apply succeeds - enable --no-reload, then the ONE daemon-reload, then start; every unit NeedDaemonReload=no after it"
  else bad "wgw WS v$SV apply rc=$RC seq=$seq_ok"; printf '%s\n' "$OUT" | grep -E 'STEP|FAIL|PENDING|ABORT' | tail -6 | sed 's/^/    /'; fi
  python3 -c 'import sys,time; print("\n".join("V5 GET %s status=%s content_type=- size=0 want=%s ok" % (p, c, c) for p, c in (("/v1/watcher/status",404),("/v1/watcher/dialogs",404),("/V1/WATCHER/status",404),("/m/v1/watcher/status",401),("/m/v1/accounts",401)))); print("V5_PUBLIC_CHECK_OK mode=external phase=unit-running host=jp-bot.balen.wang at=x epoch=%d checks=5" % (int(time.time()) + 1))' > "$S/evidence/v5-unit-running.txt"
  run_wgw verify --v5-evidence "$S/evidence/v5-unit-running.txt"
  if [ "$RC" = 0 ] && printf '%s' "$OUT" | grep -q 'CP_ISOLATION_OK units=4 watcher_gateway=present' && printf '%s' "$OUT" | grep -q '^WGW_VERIFY_OK$' \
     && printf '%s' "$OUT" | grep -q '^V5_EVIDENCE_OK'; then ok "wgw WS v$SV: verify passes (four-unit isolation, no NeedDaemonReload left, V-5 evidence)"
  else bad "wgw WS v$SV verify rc=$RC"; printf '%s\n' "$OUT" | grep -E 'STEP|FAIL|UNCOMPARABLE|INCOMPLETE|ABORT' | tail -6 | sed 's/^/    /'; fi
  run_wgw verify
  [ "$RC" != 0 ] && printf '%s' "$OUT" | grep -q '^WGW_VERIFY_INCOMPLETE DIRECT_GUARD_UNVERIFIED' && ! printf '%s' "$OUT" | grep -q '^WGW_VERIFY_OK$' \
    && ok "wgw WS v$SV: verify without external evidence is INCOMPLETE (DIRECT_GUARD_UNVERIFIED), never OK" || bad "wgw WS v$SV: verify without evidence rc=$RC"
  : > "$CALLS.mark"; echo "---- manual rollback" >> "$CALLS"
  run_wgw rollback
  if [ "$RC" = 0 ] && printf '%s' "$OUT" | grep -q 'WGW_UNIT_REMOVED' && [ ! -e "$SB/etc/systemd/system/trader-v3-controlplane-watcher-gateway.service" ] \
     && ! grep -q 'NeedDaemonReload=yes' "$S/evidence/reload-rollback-before-disable.txt" && ! grep -q 'NeedDaemonReload=yes' "$S/evidence/reload-rollback-after.txt" \
     && [ "$(sed -n '/^---- manual rollback/,$p' "$CALLS" | grep -c '^systemctl daemon-reload')" = 1 ]; then
    ok "wgw WS v$SV: manual rollback removes everything (gate on the pre-disable listing; one reload; all units back to no)"
  else bad "wgw WS v$SV rollback rc=$RC"; printf '%s\n' "$OUT" | grep -E 'STEP|PENDING|FAIL|ABORT' | tail -6 | sed 's/^/    /'; fi
  wgw_nothing_new "wgw WS v$SV (after rollback)" objects-only
done
unset SYSTEMD_VERSION O0_WGW_TEST_URL O0_OQ_TEST_URL O0_V5_TEST_PORT
kill "$FAKE_PID" 2>/dev/null || true; wait "$FAKE_PID" 2>/dev/null || true
# the pre-fix order would have failed exactly here on v255: enable AFTER the reload leaves the host-wide mark set
SYSTEMD_VERSION=255 CALLS="$WORK/calls-v255-demo.log" bash -c ': > "$CALLS"; "$0/systemctl" daemon-reload; "$0/systemctl" enable --no-reload x.service; "$0/systemctl" show -p Id -p NeedDaemonReload caddy.service' "$BIN" \
  | grep -q 'NeedDaemonReload=yes' && ok "stub: on v255 an enable --no-reload after the reload leaves every unit NeedDaemonReload=yes (the bug the new order avoids)" \
  || bad "stub: v255 host-wide mark not modelled"
# WD: NeedDaemonReload pending before apply -> refused before any write
wgw_setup WD; run_wgw preflight; : > "$CALLS"
RELOAD_PENDING=always run_wgw apply
if [ "$RC" != 0 ] && printf '%s' "$OUT" | grep -q 'DAEMON_RELOAD_PENDING units=x.timer' && ! printf '%s' "$OUT" | grep -q AUTO_ROLLBACK; then ok "wgw WD: NeedDaemonReload=yes anywhere refuses apply before any write"
else bad "wgw WD rc=$RC"; fi
wgw_nothing_new "wgw WD"
# WE: the shared dir changed between preflight and apply -> refused before any write (never repaired)
wgw_setup WE; run_wgw preflight; : > "$CALLS"
echo 'late = 1' > "$TR/packages/execution-domain/a.py"
run_wgw apply
if [ "$RC" != 0 ] && printf '%s' "$OUT" | grep -q 'SHARED_CHANGED changed=1' && [ "$(cat "$TR/packages/execution-domain/a.py")" = 'late = 1' ]; then
  ok "wgw WE: a shared-dir change after preflight refuses apply (reported, not repaired)"
else bad "wgw WE rc=$RC"; fi
echo 'x = 1' > "$TR/packages/execution-domain/a.py"; wgw_nothing_new "wgw WE"
# WR: the manual rollback phase on a clean host is a no-op that still checks the three units and the shared dir
wgw_setup WR; run_wgw preflight; : > "$CALLS"
run_wgw rollback
[ "$RC" = 0 ] && printf '%s' "$OUT" | grep -q WGW_UNIT_REMOVED && printf '%s' "$OUT" | grep -q OTHER_UNITS_UNCHANGED && printf '%s' "$OUT" | grep -q SHARED_UNCHANGED \
  && ok "wgw WR: rollback phase (nothing installed) passes and re-checks the three units and the shared dir" || { bad "wgw WR rc=$RC"; printf '%s\n' "$OUT" | tail -5; }
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
  wgw_setup "ISO-$kind"; run_wgw preflight; : > "$CALLS"
  ISOLATION_FAIL=$kind run_wgw apply
  failed_at="$(printf '%s\n' "$OUT" | awk '/\[o0\] STEP [0-9]+: /{s=$0} END{print s}' | sed -E 's/.*STEP [0-9]+: //')"
  if [ "$RC" -ne 0 ] && { [ "$want_iso" != CP_ISOLATION_FAILED ] || printf '%s' "$OUT" | grep -q 'ENVFILE_ISOLATION VIOLATION'; } && printf '%s' "$OUT" | grep -q "$want_iso" \
     && [ "${failed_at#control-plane unit isolation}" != "$failed_at" ] && [ ! -e "$S/evidence/auto-rollback.log" ]; then
    ok "wgw ISO-$kind: apply refused at the isolation gate, before any write"
  else bad "wgw ISO-$kind: rc=$RC failed_at='$failed_at'"; printf '%s\n' "$OUT" | grep -E 'STEP|ISOLATION|ABORT' | tail -5 | sed 's/^/    /'; fi
  wgw_nothing_new "wgw ISO-$kind"
  printf '%s' "$OUT" | grep -q SENTINELisoleak && bad "wgw ISO-$kind: an Environment= VALUE was printed" || ok "wgw ISO-$kind: no env value printed (names only)"
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
if grep -hE 'systemctl (restart|stop|start|kill|reload|try-restart|reload-or-restart|enable|disable) .*(node-control|event-ingest|operator-query)' "$WORK"/calls-*.log >/dev/null; then
  bad "a run touched node-control/event-ingest/operator-query"; else ok "node-control, event-ingest and operator-query never touched (read-only 'systemctl show' only)"; fi
fi

if [ "$fails" -gt 0 ]; then echo "APPLY_ROLLBACK_TEST_FAILED failures=$fails checks=$checks"; exit 1; fi
echo "APPLY_ROLLBACK_TEST_OK checks=$checks"
