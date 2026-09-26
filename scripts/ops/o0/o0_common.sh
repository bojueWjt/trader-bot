# shellcheck shell=bash
# O-0 release drafts: shared helpers. Source only; never executed directly.
#
# Modes (every o0_*.sh script):
#   --plan / --dry-run (default)  print every step and command, execute NOTHING
#   --execute --auth-id O0-Axx     run the selected phase; requires root on jp-24,
#                                  a stage dir under /srv/trader-staging/o0-*,
#                                  and an authorization id from
#                                  docs/agent-team/release/o0-authorization-list.md
# Iron rules encoded here: set -eo pipefail; no /tmp staging; no RESUME anywhere;
# no `systemctl reload caddy`; never print credential values.

# -E: ERR traps (automatic rollback) must also fire inside functions.
set -Eeo pipefail

O0_MODE="plan"
O0_AUTH_ID=""
O0_PHASE=""
O0_STAGE_DIR=""
O0_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
O0_TOOL="$O0_SCRIPT_DIR/o0_tool.py"
O0_PY="${O0_PY:-python3}"
O0_STEP_NO=0

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

o0_forbid_patterns() {
  # Defensive self-check: the scripts must never contain a RESUME call or caddy reload.
  local file pattern
  pattern='(systemctl[[:space:]]+reload[[:space:]]+caddy|caddy[[:space:]]+reload|"type"[[:space:]]*:[[:space:]]*"RESUME"|resume_race|resume_fleet)'  # o0-allow
  [ "$#" -gt 0 ] || o0_die "o0_forbid_patterns: no files given (uncomparable)"
  for file in "$@"; do
    [ -r "$file" ] || o0_die "o0_forbid_patterns: cannot read $file"
    if grep -nE "$pattern" "$file" | grep -v 'o0-allow' | grep -v '^[0-9]*:[[:space:]]*#' | grep -q .; then
      o0_die "forbidden production verb found in $file"
    fi
  done
}

o0_parse_common() {
  # Consumes common flags; echoes the remaining args into O0_REST (array).
  O0_REST=()
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --plan|--dry-run) O0_MODE="plan" ;;
      --execute) O0_MODE="execute" ;;
      --auth-id) O0_AUTH_ID="${2:-}"; shift ;;
      --phase) O0_PHASE="${2:-}"; shift ;;
      --stage-dir) O0_STAGE_DIR="${2:-}"; shift ;;
      *) O0_REST+=("$1") ;;
    esac
    shift
  done
}

o0_require_stage_dir() {
  local dir="$1"
  case "$dir" in
    /srv/trader-staging/o0-*) ;;
    *) o0_die "stage dir must be /srv/trader-staging/o0-<utc-ts> (never /tmp): '$dir'" ;;
  esac
  case "$dir" in *..*|*' '*) o0_die "stage dir must not contain '..' or spaces" ;; esac
}

o0_require_execute_context() {
  [ "$O0_MODE" = "execute" ] || return 0
  [[ "$O0_AUTH_ID" =~ ^O0-A[0-9]{2}(-[A-Za-z0-9._:-]+)?$ ]] \
    || o0_die "--auth-id must name an item of o0-authorization-list.md (O0-Axx[-suffix])"
  [ "$(id -u)" = "0" ] || o0_die "execute mode must run as root on jp-24"
  [ -d /srv/trader-v3 ] || o0_die "/srv/trader-v3 missing: this is not jp-24"
  o0_require_stage_dir "$O0_STAGE_DIR"
  [ -d "$O0_STAGE_DIR" ] || o0_die "stage dir does not exist: $O0_STAGE_DIR"
  mkdir -p -m 0700 "$O0_STAGE_DIR/evidence"
  o0_log "authorization=$O0_AUTH_ID phase=$O0_PHASE stage=$O0_STAGE_DIR"
  printf '%s phase=%s auth=%s script=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$O0_PHASE" "$O0_AUTH_ID" "$(basename "$0")" \
    >> "$O0_STAGE_DIR/evidence/authorizations.log"
}

# Fleet state snapshot (read-only). Same query as docs/agent-operations.md §0.
o0_fleet_state_cmd() {
  cat <<'EOF'
docker exec trader-v3-postgres psql -U postgres -d trader -Atc "SELECT node_id||' '||status||' hb_age='||round(extract(epoch from now()-last_seen_at),1) FROM node_heartbeats ORDER BY node_id"
EOF
}

o0_record_fleet_state() {
  local label="$1"
  o0_sh "record fleet state ($label): node status + heartbeat age (read-only)" \
    "$(o0_fleet_state_cmd) | tee \"$O0_STAGE_DIR/evidence/fleet-$label.txt\""
}

o0_compare_fleet_state() {
  local before="$1" after="$2"
  o0_sh "compare fleet status $before vs $after (status column only; any change = report to user, never RESUME)" \
    "diff <(awk '{print \$1, \$2}' \"$O0_STAGE_DIR/evidence/fleet-$before.txt\") <(awk '{print \$1, \$2}' \"$O0_STAGE_DIR/evidence/fleet-$after.txt\") && echo FLEET_STATUS_UNCHANGED || { echo FLEET_STATUS_CHANGED; exit 3; }"
}
