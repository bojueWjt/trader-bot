# shellcheck shell=bash
# O-0 release drafts: shared helpers. Source only; never executed directly.
#
# Modes (every o0_*.sh script):
#   --plan / --dry-run (default)  print every step and command, execute NOTHING
#   --execute --phase P --auth-id O0-Axx
#                                  run ONE phase; requires root on jp-24, a stage dir
#                                  under /srv/trader-staging/o0-*, and EXACTLY the
#                                  authorization id that o0-authorization-list.md
#                                  assigns to that script and phase (o0_expected_auth)
# Iron rules encoded here: set -eo pipefail; no /tmp staging; no RESUME anywhere;
# no caddy reload; never print credential values; gates are machine-checked before
# any write (o0_gate_require); a fleet change after a step stops the script (exit 3).

# -E: ERR traps (automatic rollback) must also fire inside functions.
set -Eeo pipefail

O0_MODE="plan"
O0_AUTH_ID=""
O0_PHASE=""
O0_STAGE_DIR=""
O0_ACK_DATA_LOSS=0
O0_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
O0_TOOL="$O0_SCRIPT_DIR/o0_tool.py"
O0_PY="${O0_PY:-python3}"
O0_STEP_NO=0

# Fleet guard parameters (docs/agent-operations.md §0: heartbeat age should be < 5 s).
# O0_FLEET_NODES must equal the node ids printed by site check S-00 (confirm on site).
O0_FLEET_NODES="${O0_FLEET_NODES:-account-a account-b account-c account-d}"
O0_FLEET_READY_PORTS="${O0_FLEET_READY_PORTS:-8081 8082 8083 8084}"
O0_FLEET_KNOWN_DOWN="${O0_FLEET_KNOWN_DOWN:-}"
O0_FLEET_MAX_HB_AGE="${O0_FLEET_MAX_HB_AGE:-5}"
O0_FLEET_MAX_HB_JUMP="${O0_FLEET_MAX_HB_JUMP:-5}"
# After a restart the nodes only write a fail-closed HALT with their next heartbeat once
# the channel is back; sample over a window, never once right after the restart.
O0_FLEET_SETTLE_S="${O0_FLEET_SETTLE_S:-60}"
O0_FLEET_SAMPLES="${O0_FLEET_SAMPLES:-4}"
O0_FLEET_INTERVAL_S="${O0_FLEET_INTERVAL_S:-20}"
# Production node heartbeat parameters (review wac-032-r2 🟡-7): set them to what site check
# S-00 printed (interval = DEFAULT_HEARTBEAT_INTERVAL_SECONDS of the running node image,
# timeout = control_plane.heartbeat_timeout_seconds of every node config). The defaults are
# the 67b401a values. Every --execute run checks that the guard thresholds above are
# consistent with them (o0_tool.py fleet-params) and refuses otherwise; changing any of these
# values needs the user's confirmation (o0-runbook-deploy.md §0).
O0_NODE_HB_INTERVAL_S="${O0_NODE_HB_INTERVAL_S:-2}"
O0_NODE_HB_TIMEOUT_S="${O0_NODE_HB_TIMEOUT_S:-15}"
# Gate freshness (AGENTS.md evidence freshness: fault report 1 h, capacity evidence 24 h).
O0_GATE_MAX_AGE_S="${O0_GATE_MAX_AGE_S:-3600}"
O0_BUILD_GATE_MAX_AGE_S="${O0_BUILD_GATE_MAX_AGE_S:-86400}"

o0_log() { printf '%s [o0] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
o0_die() { o0_log "ABORT: $*"; exit 1; }

o0_quote() {
  local out="" a
  for a in "$@"; do out+="$(printf '%q' "$a") "; done
  printf '%s' "${out% }"
}

# o0_step "description" cmd args...   (plan: print; execute: log + run)
o0_step() {
  local desc="$1"
  shift
  O0_STEP_NO=$((O0_STEP_NO + 1))
  if [ "$O0_MODE" = "execute" ]; then
    o0_log "STEP $O0_STEP_NO: $desc"
    o0_log "  \$ $(o0_quote "$@")"
    "$@"
  else
    printf 'PLAN %02d  %s\n         $ %s\n' "$O0_STEP_NO" "$desc" "$(o0_quote "$@")"
  fi
}

# o0_sh "description" 'shell text'   (for pipelines; runs under bash -eo pipefail)
o0_sh() {
  local desc="$1"
  local text="$2"
  if [ "$O0_MODE" = "execute" ]; then
    o0_step "$desc" bash -o pipefail -ec "$text"
  else
    O0_STEP_NO=$((O0_STEP_NO + 1))
    printf 'PLAN %02d  %s\n         $ bash -o pipefail -ec <<<\n%s\n' "$O0_STEP_NO" "$desc" "$(printf '%s\n' "$text" | sed 's/^[[:space:]]*/           /')"
  fi
}

o0_note() {
  if [ "$O0_MODE" = "execute" ]; then o0_log "NOTE: $*"; else printf 'NOTE     %s\n' "$*"; fi
}

# Forbidden production verbs (G10 and the scripts' own self-check). Lines carrying
# the marker o0-allow and shell comment lines are exempt.
O0_FORBIDDEN_RE='(systemctl[[:space:]]+reload[[:space:]]+caddy|caddy[[:space:]]+reload|caddy[[:space:]]+load|:2019/load|"type"[[:space:]]*:[[:space:]]*"RESUME"|resume_race|resume_fleet|/v1/commands|systemctl[[:space:]]+(restart|stop|kill|try-restart|reload-or-restart)[[:space:]]+trader-v3-controlplane-(node-control|event-ingest)|docker[[:space:]-]+compose[^|;&]*[[:space:]](down|restart|kill|rm)([[:space:]]|$)|docker[[:space:]]+(stop|kill|restart)[[:space:]]+trader-v3-(node|nautilus))'  # o0-allow

o0_forbid_patterns() {
  local file hits
  [ "$#" -gt 0 ] || o0_die "o0_forbid_patterns: no files given (uncomparable)"
  for file in "$@"; do
    [ -r "$file" ] || o0_die "o0_forbid_patterns: cannot read $file"
    # grep -c instead of grep -q at the end of a pipeline (SIGPIPE under pipefail, R77)
    hits=$(grep -nE "$O0_FORBIDDEN_RE" "$file" | grep -v 'o0-allow' | grep -vE '^[0-9]+:[[:space:]]*#' | grep -c . || true)
    [ "$hits" = "0" ] || o0_die "forbidden production verb found in $file ($hits line(s))"
  done
}

o0_parse_common() {
  # Consumes common flags; leaves the remaining args in O0_REST (array).
  O0_REST=()
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --plan|--dry-run) O0_MODE="plan" ;;
      --execute) O0_MODE="execute" ;;
      --auth-id) O0_AUTH_ID="${2:-}"; shift ;;
      --phase) O0_PHASE="${2:-}"; shift ;;
      --stage-dir) O0_STAGE_DIR="${2:-}"; shift ;;
      --i-understand-data-loss) O0_ACK_DATA_LOSS=1 ;;
      *) O0_REST+=("$1") ;;
    esac
    shift
  done
}

# The ONE authorization id each (script, phase) accepts. Source of truth:
# docs/agent-team/release/o0-authorization-list.md. tests/auth_binding_test.sh checks
# every example in the runbooks against this table.
o0_expected_auth() {
  case "$1:$2" in
    o0_deploy_caddy.sh:preflight|o0_deploy_caddy.sh:apply|o0_deploy_caddy.sh:verify|o0_deploy_caddy.sh:rollback) echo "O0-A05" ;;
    o0_deploy_watcher.sh:preflight|o0_deploy_watcher.sh:build) echo "O0-A04" ;;
    o0_deploy_watcher.sh:apply-preflight|o0_deploy_watcher.sh:apply|o0_deploy_watcher.sh:verify|o0_deploy_watcher.sh:rollback) echo "O0-A07" ;;
    o0_deploy_watcher.sh:restore-db) echo "O0-A07R" ;;
    o0_deploy_watcher_gateway.sh:preflight|o0_deploy_watcher_gateway.sh:apply|o0_deploy_watcher_gateway.sh:verify|o0_deploy_watcher_gateway.sh:rollback) echo "O0-A08" ;;
    # standalone fleet guard: the phase is the runbook step it guards
    o0_fleet_guard.sh:R-1) echo "O0-A20" ;;
    o0_fleet_guard.sh:R-2) echo "O0-A21" ;;
    o0_fleet_guard.sh:R-3|o0_fleet_guard.sh:R-4) echo "O0-A22" ;;
    o0_fleet_guard.sh:R-5) echo "O0-A23" ;;
    o0_fleet_guard.sh:drill-R-2|o0_fleet_guard.sh:drill-R-3|o0_fleet_guard.sh:drill-R-5) echo "O0-A11" ;;
    o0_fleet_guard.sh:SW-2) echo "O0-A13" ;;
    o0_fleet_guard.sh:SW-3|o0_fleet_guard.sh:SW-5) echo "O0-A14" ;;
    o0_fleet_guard.sh:SW-smoke) echo "O0-A15" ;;
    o0_fleet_guard.sh:SW-drill) echo "O0-A16" ;;
    o0_fleet_guard.sh:SW-4) echo "O0-A17" ;;
    *) echo "" ;;
  esac
}

# O0_SANDBOX: local test seam only. Refused on any host that has /srv/trader-v3, so it
# can never relax the checks on jp-24. Moves the jp-24 marker and staging root into the
# sandbox; `id` is stubbed by the tests.
o0_host_roots() {
  if [ -n "${O0_SANDBOX:-}" ]; then
    [ ! -e /srv/trader-v3 ] || o0_die "O0_SANDBOX is refused on a host that has /srv/trader-v3"
    O0_MARKER="$O0_SANDBOX/srv/trader-v3"
    O0_STAGING_ROOT="$O0_SANDBOX/srv/trader-staging"
    o0_log "SANDBOX MODE (local tests): marker=$O0_MARKER staging=$O0_STAGING_ROOT"
  else
    O0_MARKER="/srv/trader-v3"
    O0_STAGING_ROOT="/srv/trader-staging"
  fi
}

# O0_SANDBOX only: host paths that have no command-line flag (e.g. /etc/caddy/Caddyfile)
# move under the sandbox root. No effect without O0_SANDBOX; refused on jp-24.
o0_sandbox_paths() {
  [ -n "${O0_SANDBOX:-}" ] || return 0
  [ ! -e /srv/trader-v3 ] || o0_die "O0_SANDBOX is refused on a host that has /srv/trader-v3"
  local v
  for v in "$@"; do printf -v "$v" '%s' "$O0_SANDBOX${!v}"; done
}

o0_require_stage_dir() {
  local dir="$1"
  o0_host_roots
  case "$dir" in
    "$O0_STAGING_ROOT"/o0-*) ;;
    *) o0_die "stage dir must be $O0_STAGING_ROOT/o0-<utc-ts> (never /tmp): '$dir'" ;;
  esac
  case "$dir" in *..*|*' '*) o0_die "stage dir must not contain '..' or spaces" ;; esac
}

# Checks the (script, phase) -> authorization binding. Pure: usable in tests.
o0_check_auth_binding() {
  local script="$1" phase="$2" auth="$3" ack="$4" expected
  [ -n "$phase" ] || { echo "--phase is required with --execute"; return 1; }
  expected="$(o0_expected_auth "$script" "$phase")"
  [ -n "$expected" ] || { echo "no authorization item covers $script phase '$phase'"; return 1; }
  [ "$auth" = "$expected" ] || { echo "$script phase $phase needs --auth-id $expected (o0-authorization-list.md), got '${auth:-<none>}'"; return 1; }
  if [ "$expected" = "O0-A07R" ] && [ "$ack" != "1" ]; then
    echo "restore-db is destructive (config writes and Telegram messages after apply are lost): add --i-understand-data-loss"
    return 1
  fi
  return 0
}

o0_require_execute_context() {
  [ "$O0_MODE" = "execute" ] || return 0
  local script msg
  script="$(basename "$0")"
  msg="$(o0_check_auth_binding "$script" "$O0_PHASE" "$O0_AUTH_ID" "$O0_ACK_DATA_LOSS")" || o0_die "$msg"
  [ "$(id -u)" = "0" ] || o0_die "execute mode must run as root on jp-24"
  o0_host_roots
  [ -d "$O0_MARKER" ] || o0_die "$O0_MARKER missing: this is not jp-24"
  o0_require_stage_dir "$O0_STAGE_DIR"
  [ -d "$O0_STAGE_DIR" ] || o0_die "stage dir does not exist: $O0_STAGE_DIR"
  mkdir -p -m 0700 "$O0_STAGE_DIR/evidence"
  o0_fleet_params_check
  local cand="<no-bundle>"
  if [ -r "$O0_STAGE_DIR/bundle/RELEASE.json" ]; then
    cand="$("$O0_PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("candidate","?"))' "$O0_STAGE_DIR/bundle/RELEASE.json")"
  fi
  o0_log "authorization=$O0_AUTH_ID script=$script phase=$O0_PHASE candidate=$cand stage=$O0_STAGE_DIR"
  printf '%s script=%s phase=%s auth=%s candidate=%s ack_data_loss=%s fleet_params=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$script" "$O0_PHASE" \
    "$O0_AUTH_ID" "$cand" "$O0_ACK_DATA_LOSS" "$(o0_fleet_params_summary)" >> "$O0_STAGE_DIR/evidence/authorizations.log"
}

o0_fleet_params_summary() {
  printf 'hb_interval=%s,hb_timeout=%s,max_hb_age=%s,max_hb_jump=%s,settle=%s,samples=%sx%s' "$O0_NODE_HB_INTERVAL_S" "$O0_NODE_HB_TIMEOUT_S" \
    "$O0_FLEET_MAX_HB_AGE" "$O0_FLEET_MAX_HB_JUMP" "$O0_FLEET_SETTLE_S" "$O0_FLEET_SAMPLES" "$O0_FLEET_INTERVAL_S"
}

# Guard thresholds vs production node heartbeat parameters (review wac-032-r2 🟡-7), before
# any step of any execute run. O0_FLEET_PARAMS_SANDBOX_SKIP is honoured only together with
# O0_SANDBOX (itself refused on jp-24): the sandbox tests use sub-second windows.
o0_fleet_params_check() {
  local out rc=0
  if [ -n "${O0_SANDBOX:-}" ] && [ "${O0_FLEET_PARAMS_SANDBOX_SKIP:-}" = 1 ]; then
    [ ! -e /srv/trader-v3 ] || o0_die "O0_SANDBOX is refused on a host that has /srv/trader-v3"
    o0_log "SANDBOX: fleet parameter check skipped (sub-second test windows)"
    return 0
  fi
  out="$("$O0_PY" "$O0_TOOL" fleet-params --hb-interval-s "$O0_NODE_HB_INTERVAL_S" --hb-timeout-s "$O0_NODE_HB_TIMEOUT_S" \
    --max-hb-age "$O0_FLEET_MAX_HB_AGE" --max-hb-jump "$O0_FLEET_MAX_HB_JUMP" --settle-s "$O0_FLEET_SETTLE_S" \
    --samples "$O0_FLEET_SAMPLES" --sample-interval-s "$O0_FLEET_INTERVAL_S" 2>&1)" || rc=$?
  while IFS= read -r line; do o0_log "$line"; done <<< "$out"
  [ "$rc" = 0 ] || o0_die "fleet guard thresholds are inconsistent with the node heartbeat parameters (S-00): adjust per o0-runbook-deploy.md §0, only with the user's confirmation"
}

# ---------------------------------------------------------------- fleet guard
# One sample = the docs/agent-operations.md §0 query (status, release, heartbeat age per
# node) + the four /ready codes. NULLs are printed as NULL so the parser refuses them.
o0_fleet_sample_text() {
  local out="$1"
  cat <<EOF
{ docker exec trader-v3-postgres psql -U postgres -d trader -Atc "SELECT node_id||' '||coalesce(status::text,'NULL')||' '||coalesce(release_id::varchar(12),'NULL')||' hb_age='||coalesce(round(extract(epoch from now()-last_seen_at)::numeric,1)::text,'NULL') FROM node_heartbeats ORDER BY node_id" </dev/null
  for p in $O0_FLEET_READY_PORTS; do printf 'ready:%s %s\n' "\$p" "\$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:\$p/ready </dev/null || true)"; done
} > "$out"
EOF
}

o0_fleet_args() {
  printf '%s' "--nodes $(printf '%q' "$O0_FLEET_NODES") --ready-ports $(printf '%q' "$O0_FLEET_READY_PORTS") --known-down $(printf '%q' "$O0_FLEET_KNOWN_DOWN") --max-hb-age $O0_FLEET_MAX_HB_AGE --max-hb-jump $O0_FLEET_MAX_HB_JUMP"
}

# Record a baseline sample and require it to be complete and fresh (exact node set,
# every heartbeat < max age). Used as the FIRST step of every apply.
o0_fleet_baseline() {
  local label="$1" f="$O0_STAGE_DIR/evidence/fleet-$1.txt"
  o0_sh "fleet baseline ($label): exact node set, heartbeat age < ${O0_FLEET_MAX_HB_AGE}s, /ready codes (read-only)" \
    "$(o0_fleet_sample_text "$f")
     $(o0_quote "$O0_PY" "$O0_TOOL") fleet-compare --before '$f' $(o0_fleet_args) | tee '$O0_STAGE_DIR/evidence/fleet-$label.verdict.txt'"
}

# Record a sample WITHOUT gating on it (rollbacks must never be blocked by fleet state).
o0_fleet_record() {
  local label="$1" f="$O0_STAGE_DIR/evidence/fleet-$1.txt"
  o0_sh "fleet sample $label (recorded, not gated: a rollback must not be blocked by fleet state)" \
    "$(o0_fleet_sample_text "$f")
     cat '$f'"
}

# Wait the settle window, then take N samples; every one must equal the baseline in
# status, release and /ready, with a live heartbeat. 0 rows or a partial set = exit 2;
# change = exit 3. Never RESUMEs, never rolls back (a rollback would restart again).
o0_fleet_settle_compare() {
  local before="$1" label="$2"
  local bf="$O0_STAGE_DIR/evidence/fleet-$before.txt"
  local text
  text="sleep $O0_FLEET_SETTLE_S
     i=1
     while [ \$i -le $O0_FLEET_SAMPLES ]; do
       $(o0_fleet_sample_text "$O0_STAGE_DIR/evidence/fleet-$label-\$i.txt")
       rc=0
       $(o0_quote "$O0_PY" "$O0_TOOL") fleet-compare --before '$bf' --after '$O0_STAGE_DIR/evidence/fleet-$label-'\$i'.txt' $(o0_fleet_args) \\
         > '$O0_STAGE_DIR/evidence/fleet-$label-'\$i'.verdict.txt' || rc=\$?
       cat '$O0_STAGE_DIR/evidence/fleet-$label-'\$i'.verdict.txt'
       if [ \$rc -ne 0 ]; then echo \"FLEET_GUARD_STOP sample=\$i rc=\$rc: report to the user; no RESUME, no rollback\"; exit \$rc; fi
       if [ \$i -lt $O0_FLEET_SAMPLES ]; then sleep $O0_FLEET_INTERVAL_S; fi
       i=\$((i + 1))
     done
     echo FLEET_UNCHANGED_ALL_SAMPLES samples=$O0_FLEET_SAMPLES window_s=\$(( $O0_FLEET_SETTLE_S + ($O0_FLEET_SAMPLES - 1) * $O0_FLEET_INTERVAL_S ))"
  o0_sh "fleet after $label: wait ${O0_FLEET_SETTLE_S}s, then $O0_FLEET_SAMPLES samples every ${O0_FLEET_INTERVAL_S}s vs $before (any change = exit 3: stop, report, never RESUME)" "$text"
}

# ---------------------------------------------------------------- automatic rollback
# Review wac-032-r2 🟡-1. An apply first changes FILES, then replaces the RUNNING service
# (Caddy restart, watcher recreate, watcher-gateway start). If it fails before the
# replacement, the running service never changed: restoring the files is enough, and a
# restart would only add risk (D-02: a Caddy restart has HALTed the whole fleet before).
# O0_RUNTIME_REPLACED is set immediately BEFORE the replacing step (a failed restart may
# already have changed the service, so "attempted" is what counts).
O0_RUNTIME_REPLACED=0

o0_mark_runtime_replaced() {
  O0_RUNTIME_REPLACED=1
  o0_note "RUNTIME_REPLACEMENT_BEGINS: from here on the automatic rollback also restarts/recreates"
}

# o0_arm_auto_rollback <what> <files-fn> <runtime-fn> <fleet-baseline-label>
o0_arm_auto_rollback() {
  if [ "$O0_MODE" = "execute" ]; then
    # shellcheck disable=SC2064
    trap "o0_auto_rollback $(o0_quote "$1" "$2" "$3" "$4")" ERR
  else
    o0_note "AUTO_ROLLBACK_ARMED ($1): on any failure below restore files ($2); run $3 only after RUNTIME_REPLACEMENT_BEGINS; then fleet guard vs $4 (report only)"
  fi
}

# ERR-trap body. Files are always restored (fail-fast: if that fails nothing is restarted);
# the runtime is restarted/recreated only when O0_RUNTIME_REPLACED=1. Afterwards the fleet
# is sampled against the apply baseline and the verdict is recorded in evidence (report
# only: never RESUME, never a second rollback). Always exits 1.
o0_auto_rollback() {
  local what="$1" files_fn="$2" runtime_fn="$3" before="$4" rc=0 frc=0 restarted=no
  trap - ERR
  set +e
  o0_log "apply failed: automatic rollback of $what (runtime_replaced=$O0_RUNTIME_REPLACED)"
  ( set -e; "$files_fn" )
  rc=$?
  if [ "$rc" -eq 0 ] && [ "$O0_RUNTIME_REPLACED" = 1 ]; then
    restarted=yes
    ( set -e; "$runtime_fn" )
    rc=$?
  elif [ "$rc" -eq 0 ]; then
    o0_log "running $what was never replaced: files restored, NO restart/recreate"
  fi
  [ "$rc" -eq 0 ] || o0_log "ROLLBACK FAILED rc=$rc: escalate to user now"
  ( set -e; o0_fleet_settle_compare "$before" "after-auto-rollback" )
  frc=$?
  printf '%s what=%s runtime_replaced=%s runtime_rolled_back=%s rollback_rc=%s fleet_rc=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$what" "$O0_RUNTIME_REPLACED" "$restarted" "$rc" "$frc" >> "$O0_STAGE_DIR/evidence/auto-rollback.log"
  o0_log "AUTO_ROLLBACK_DONE what=$what runtime_rolled_back=$restarted rollback_rc=$rc fleet_rc=$frc (0 unchanged, 2 uncomparable, 3 CHANGED: report to the user; never RESUME)"
  exit 1
}

# ---------------------------------------------------------------- gates
o0_gate_file() { printf '%s' "$O0_STAGE_DIR/evidence/$1.gate.json"; }

o0_gate_clear() {
  o0_step "clear any earlier $1 gate record (it is rewritten only if every check below passes)" rm -f "$(o0_gate_file "$1")"
}

# o0_gate_write <stage> [--field k=v] [--file-sha k=path] ...   (last step of a gate phase)
o0_gate_write() {
  local stage="$1"
  shift
  o0_step "record the passed $stage gate, bound to this bundle and inputs" \
    "$O0_PY" "$O0_TOOL" gate-write --out "$(o0_gate_file "$stage")" --stage "$stage" --bundle "$O0_STAGE_DIR/bundle" "$@"
}

# o0_gate_require <stage> <max-age-s> [--expect k=v] [--expect-file-sha k=path] ...  (first step of apply)
o0_gate_require() {
  local stage="$1" max_age="$2"
  shift 2
  o0_step "REQUIRE passed $stage gate for this candidate and inputs (age <= ${max_age}s) before ANY change" \
    "$O0_PY" "$O0_TOOL" gate-check --gate "$(o0_gate_file "$stage")" --stage "$stage" --bundle "$O0_STAGE_DIR/bundle" --max-age-s "$max_age" "$@"
}

# Cheap gates re-run inside apply (the bundle in hand is intact and still a candidate).
o0_bundle_recheck() {
  o0_sh "bundle integrity (SHA256SUMS) and RELEASE.json deploy_candidate=true, phase_max=P2" \
    "cd '$O0_STAGE_DIR/bundle' && sha256sum -c --quiet SHA256SUMS && $(o0_quote "$O0_PY") -c 'import json,sys; r=json.load(open(\"RELEASE.json\")); print(\"candidate\", r[\"candidate\"], \"deploy_candidate\", r[\"deploy_candidate\"]); sys.exit(0 if r[\"deploy_candidate\"] is True and r[\"phase_max\"]==\"P2\" else 1)'"
}
