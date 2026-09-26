#!/usr/bin/env bash
# Offline tests for review wac-032 🔴-2 (authorization bound to script+phase) and 🔴-3
# (apply refuses without a matching gate; the watcher candidate image must exist before
# anything is stopped). Runs the REAL scripts with --execute inside O0_SANDBOX (refused on
# any host that has /srv/trader-v3) with stubbed `id` and `docker`; asserts that every
# refusal happens before any write outside the sandbox evidence directory.
set -eo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
O0="$HERE/.."
REPO="$(cd "$O0/../../.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/o0-authgate.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
[ ! -e /srv/trader-v3 ] || { echo "SKIP: this host has /srv/trader-v3 (the sandbox is refused here by design)"; exit 0; }
fails=0; checks=0
ok() { checks=$((checks + 1)); echo "PASS $*"; }
bad() { checks=$((checks + 1)); fails=$((fails + 1)); echo "FAIL $*"; }

# ---------------------------------------------------------------- 1. binding table (pure)
# shellcheck source=../o0_common.sh
( source "$O0/o0_common.sh"
  ALL="O0-A01 O0-A02 O0-A03 O0-A04 O0-A05 O0-A06 O0-A07 O0-A07R O0-A08 O0-A10 O0-A11 O0-A12 O0-A13 O0-A14 O0-A15 O0-A16 O0-A17 O0-A18 O0-A20 O0-A21 O0-A22 O0-A23"
  n=0; f=0
  for pair in o0_deploy_caddy.sh:preflight o0_deploy_caddy.sh:apply o0_deploy_caddy.sh:verify o0_deploy_caddy.sh:rollback \
              o0_deploy_watcher.sh:preflight o0_deploy_watcher.sh:build o0_deploy_watcher.sh:apply-preflight o0_deploy_watcher.sh:apply \
              o0_deploy_watcher.sh:verify o0_deploy_watcher.sh:rollback o0_deploy_watcher.sh:restore-db \
              o0_deploy_operator_query.sh:preflight o0_deploy_operator_query.sh:apply o0_deploy_operator_query.sh:verify o0_deploy_operator_query.sh:rollback \
              o0_fleet_guard.sh:R-2 o0_fleet_guard.sh:R-3 o0_fleet_guard.sh:SW-3 o0_fleet_guard.sh:SW-4; do
    script="${pair%%:*}"; phase="${pair#*:}"; want="$(o0_expected_auth "$script" "$phase")"
    [ -n "$want" ] || { echo "FAIL no id for $pair"; f=$((f + 1)); continue; }
    for id in $ALL "" "O0-A05-x" "o0-a05"; do
      n=$((n + 1))
      if o0_check_auth_binding "$script" "$phase" "$id" 1 >/dev/null; then
        [ "$id" = "$want" ] || { echo "FAIL $pair accepted $id (want $want)"; f=$((f + 1)); }
      else
        [ "$id" != "$want" ] || { echo "FAIL $pair refused its own id $id"; f=$((f + 1)); }
      fi
    done
  done
  o0_check_auth_binding o0_deploy_watcher.sh restore-db O0-A07R 0 >/dev/null && { echo "FAIL restore-db ran without --i-understand-data-loss"; f=$((f + 1)); }
  o0_check_auth_binding o0_deploy_watcher.sh restore-db O0-A07 1 >/dev/null && { echo "FAIL restore-db accepted the deploy id O0-A07"; f=$((f + 1)); }
  o0_check_auth_binding o0_deploy_caddy.sh "" O0-A05 0 >/dev/null && { echo "FAIL empty phase accepted"; f=$((f + 1)); }
  o0_check_auth_binding o0_deploy_caddy.sh bogus O0-A05 0 >/dev/null && { echo "FAIL unknown phase accepted"; f=$((f + 1)); }
  echo "binding_matrix=$n failures=$f"; [ "$f" = 0 ]
) > "$WORK/matrix.txt" 2>&1 && ok "binding matrix: $(tail -1 "$WORK/matrix.txt")" || { bad "binding matrix"; cat "$WORK/matrix.txt"; }

# ---------------------------------------------------------------- 2. every runbook example passes the table
python3 - "$REPO/docs/agent-team/release" "$O0/o0_common.sh" > "$WORK/examples.txt" 2>&1 <<'PY' && ok "runbook examples: $(tail -1 "$WORK/examples.txt")" || { bad "runbook examples"; cat "$WORK/examples.txt"; }
import re, subprocess, sys
from pathlib import Path
docs, common = Path(sys.argv[1]), sys.argv[2]
cmd_re = re.compile(r"`([^`]*--auth-id[^`]*)`")
checked = problems = 0
for md in sorted(docs.glob("*.md")):
    for lineno, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
        for cmd in cmd_re.findall(line):
            script = re.search(r"(o0_[a-z_]+\.sh)", cmd)
            auth = re.search(r"--auth-id\s+(\S+)", cmd)
            phase = re.search(r"--phase\s+(\S+)", cmd)
            where = f"{md.name}:{lineno}"
            if not (script and auth and phase):
                if script and script.group(1) == "o0_site_check.sh" and auth and auth.group(1) == "O0-A01":
                    checked += 1
                    continue
                print(f"UNVERIFIABLE {where}: {cmd[:90]}")
                problems += 1
                continue
            ack = "1" if "--i-understand-data-loss" in cmd else "0"
            r = subprocess.run(["bash", "-c", 'source "$1"; o0_check_auth_binding "$2" "$3" "$4" "$5"', "_", common,
                                script.group(1), phase.group(1), auth.group(1), ack], capture_output=True, text=True)
            checked += 1
            if r.returncode != 0:
                print(f"REFUSED {where}: {r.stdout.strip()}")
                problems += 1
if checked < 20:
    print(f"only {checked} examples found (uncomparable)")
    sys.exit(1)
print(f"examples_checked={checked} problems={problems}")
sys.exit(1 if problems else 0)
PY

# ---------------------------------------------------------------- 3. sandbox end-to-end refusals
SB="$WORK/sb"; S="$SB/srv/trader-staging/o0-20260926T000000Z"
mkdir -p "$SB/srv/trader-v3" "$S/bundle/tools" "$S/creds/set-initial" "$WORK/bin" "$WORK/wroot/services/telegram-watcher"
printf '#!/usr/bin/env bash\n[ "$1" = "-u" ] && echo "${STUB_UID:-0}" || /usr/bin/id "$@"\n' > "$WORK/bin/id"
cat > "$WORK/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$DOCKER_LOG"
case "$*" in "image inspect"*) echo "Error: No such image" >&2; exit 1;; esac
exit 0
STUB
chmod +x "$WORK/bin/id" "$WORK/bin/docker"
cp "$O0"/o0_*.py "$S/bundle/tools/"
printf '{"candidate": "%040d", "deploy_candidate": true, "phase_max": "P2"}\n' 0 > "$S/bundle/RELEASE.json"
echo "0  x" > "$S/bundle/SHA256SUMS"
: > "$S/creds/set-initial/watcher.env"; : > "$S/creds/set-initial/caddy.env"; : > "$S/creds/set-initial/operator-query.env"
echo "services: {}" > "$WORK/wroot/docker-compose.yml"
export DOCKER_LOG="$WORK/docker.log"; : > "$DOCKER_LOG"

run() {  # run <expect-substring> <script> args...   -> expects exit != 0 and the substring
  local expect="$1" script="$2"; shift 2
  local out rc=0
  out="$(PATH="$WORK/bin:$PATH" O0_SANDBOX="$SB" bash "$O0/$script" "$@" --stage-dir "$S" 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -qF -- "$expect"; then ok "$script $* -> refused ($expect)"
  else bad "$script $* -> rc=$rc, expected refusal containing '$expect'"; printf '%s\n' "$out" | tail -5 | sed 's/^/    /'; fi
}
snapshot() { (cd "$SB" && find . -type f | sort | grep -v '/evidence/authorizations.log$'); }
before="$(snapshot)"
run "needs --auth-id O0-A05" o0_deploy_caddy.sh --execute --phase apply --auth-id O0-A01
run "needs --auth-id O0-A07R" o0_deploy_watcher.sh --execute --phase restore-db --auth-id O0-A07 --i-understand-data-loss
run "--i-understand-data-loss" o0_deploy_watcher.sh --execute --phase restore-db --auth-id O0-A07R
run "needs --auth-id O0-A08" o0_deploy_operator_query.sh --execute --phase apply --auth-id O0-A07
run "--phase is required" o0_deploy_operator_query.sh --execute --auth-id O0-A08
STUB_UID=1000 run "must run as root" o0_deploy_caddy.sh --execute --phase apply --auth-id O0-A05
run "gate file missing" o0_deploy_caddy.sh --execute --phase apply --auth-id O0-A05
run "gate file missing" o0_deploy_operator_query.sh --execute --phase apply --auth-id O0-A08 --operator-query-env "$WORK/wroot/docker-compose.yml"
run "gate file missing" o0_deploy_watcher.sh --execute --phase apply --auth-id O0-A07 --watcher-root "$WORK/wroot"
# a passed preflight gate for the watcher, but the build never ran: the image check must refuse
python3 "$O0/o0_tool.py" gate-write --out "$S/evidence/watcher-preflight.gate.json" --stage watcher-preflight --bundle "$S/bundle" \
  --file-sha "cred_watcher_env=$S/creds/set-initial/watcher.env" --file-sha "live_compose=$WORK/wroot/docker-compose.yml" >/dev/null
run "CANDIDATE_IMAGE_MISSING" o0_deploy_watcher.sh --execute --phase apply --auth-id O0-A07 --watcher-root "$WORK/wroot"
# the preflight gate exists but the credential set changed since: refuse
echo "WATCHER_GATEWAY_TOKEN=changed" > "$S/creds/set-initial/watcher.env"
run "differs from the value in hand" o0_deploy_watcher.sh --execute --phase apply --auth-id O0-A07 --watcher-root "$WORK/wroot"
: > "$S/creds/set-initial/watcher.env"
# a gate for another candidate: refuse
printf '{"candidate": "%040d", "deploy_candidate": true, "phase_max": "P2"}\n' 1 > "$S/bundle/RELEASE.json"
run "differs between the gate and the bundle" o0_deploy_watcher.sh --execute --phase apply --auth-id O0-A07 --watcher-root "$WORK/wroot"
after="$(snapshot)"
if [ "$before" = "$after" ] || [ "$(diff <(echo "$before") <(echo "$after") | grep '^>' | grep -v '/evidence/' | grep -c . || true)" = 0 ]; then
  ok "no file outside evidence/ was created by any refused run"
else bad "refused runs created files"; diff <(echo "$before") <(echo "$after") | sed 's/^/    /'; fi
if grep -Eq 'docker (tag|compose|stop|run|rm)' "$DOCKER_LOG"; then bad "a refused run called a state-changing docker verb"; cat "$DOCKER_LOG"; else ok "no state-changing docker call in any refused run"; fi
grep -q 'script=o0_deploy_watcher.sh phase=apply auth=O0-A07 candidate=' "$S/evidence/authorizations.log" && ok "authorizations.log records script, phase, id and candidate" || bad "authorizations.log format"

# ---------------------------------------------------------------- 4. step order (review wac-032-r2 🟡-2)
# gate cleared first in preflight/build, gate required first in apply, fleet guard around the
# restart, graceful dry-run stop, backup-API copy (plan output of the real scripts)
if bash "$HERE/apply_rollback_test.sh" --structure-only > "$WORK/structure.txt" 2>&1; then ok "step order: $(grep -o 'STRUCTURE_OK.*' "$WORK/structure.txt")"
else bad "step order"; cat "$WORK/structure.txt"; fi

if [ "$fails" -gt 0 ]; then echo "AUTH_GATE_TEST_FAILED failures=$fails checks=$checks"; exit 1; fi
echo "AUTH_GATE_TEST_OK checks=$checks"
