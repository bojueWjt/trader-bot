#!/usr/bin/env bash
# O-0 stage C: Caddy (browser clear+inject, /m/v1/watcher/* per-path routes). DRAFT.
# Runbook: docs/agent-team/release/o0-runbook-deploy.md stage C.
#
# Phases:  preflight (read-only) | apply (O0-A05) | verify (read-only) | rollback (O0-A05)
# Plan:    bash o0_deploy_caddy.sh --stage-dir /srv/trader-staging/o0-<ts> [--phase X]
# Execute: bash o0_deploy_caddy.sh --execute --auth-id O0-A05 --phase apply --stage-dir ... \
#            --candidate <stage>/caddy/Caddyfile.candidate --cred-set <stage>/creds/set-initial
#
# RISK (declared, not hidden): `systemctl restart caddy` briefly interrupts the node
# channel 172.30.1.1:8080 -> control plane. On 2026-08-30 this HALTed the whole fleet;
# on 2026-08-31 it did not. This script records the fleet state before/after and
# STOPS on any change. It never resumes anything: recovery is the user's own RESUME.
set -eo pipefail
# shellcheck source=o0_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/o0_common.sh"
o0_parse_common "$@"
set -- "${O0_REST[@]}"
CANDIDATE="" CRED_SET="" HOST="jp-bot.balen.wang"
CADDYFILE="/etc/caddy/Caddyfile" CADDY_ENV="/etc/caddy/v3.env"
OQ_ENV="/srv/trader-v3/secrets/control-plane/operator-query.env"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --candidate) CANDIDATE="$2"; shift ;;
    --cred-set) CRED_SET="$2"; shift ;;
    --host) HOST="$2"; shift ;;
    --operator-query-env) OQ_ENV="$2"; shift ;;
    *) o0_die "unknown argument: $1" ;;
  esac
  shift
done
O0_STAGE_DIR="${O0_STAGE_DIR:-/srv/trader-staging/o0-YYYYMMDDTHHMMSSZ}"
CANDIDATE="${CANDIDATE:-$O0_STAGE_DIR/caddy/Caddyfile.candidate}"
CRED_SET="${CRED_SET:-$O0_STAGE_DIR/creds/set-initial}"
BUNDLE="$O0_STAGE_DIR/bundle"
TOOLS="$BUNDLE/tools"
BK="$O0_STAGE_DIR/backup-caddy"
EV="$O0_STAGE_DIR/evidence"
ENVX=("$O0_PY" "$TOOLS/o0_tool.py" env-exec --env-file "$CADDY_ENV" --env-file "$CRED_SET/caddy.env" --)
[ "$O0_MODE" = "execute" ] && [ -z "$O0_PHASE" ] && o0_die "--phase is required with --execute"
o0_require_execute_context

probe_public() {
  # no credentials; resolves the public host to the local Caddy
  local label="$1"
  o0_sh "public probes via local Caddy ($label): mobile Authorization kept, browser paths behind basic auth" \
    "set -e
     c() { curl -s -o /dev/null -w '%{http_code}' -m 10 --resolve $HOST:443:127.0.0.1 \"\$@\"; }
     r1=\$(c https://$HOST/m/v1/accounts); echo \"GET /m/v1/accounts (no token) -> \$r1 (expect 401: Authorization not injected)\"; [ \"\$r1\" = 401 ]
     r2=\$(c -H 'Authorization: Bearer o0-probe-not-a-token-000000000000000000000000' https://$HOST/m/v1/accounts); echo \"GET /m/v1/accounts (bogus) -> \$r2 (expect 403)\"; [ \"\$r2\" = 403 ]
     r3=\$(c https://$HOST/m/v1/watcher/status); echo \"GET /m/v1/watcher/status (no token) -> \$r3 (expect 404 before stage O, 401 after)\"; case \"\$r3\" in 401|404) ;; *) exit 1;; esac
     r4=\$(c https://$HOST/watcher/); echo \"GET /watcher/ -> \$r4 (expect 401 basic auth)\"; [ \"\$r4\" = 401 ]
     r5=\$(c https://$HOST/api/status); echo \"GET /api/status -> \$r5 (expect 401 basic auth)\"; [ \"\$r5\" = 401 ]
     r6=\$(curl -s -o /dev/null -w '%{http_code} %{content_type}' -m 10 --resolve $HOST:443:127.0.0.1 https://$HOST/media/1700000000000-1.jpg); echo \"GET /media/<name> -> \$r6 (401, or the SPA; never an image without auth)\"; case \"\$r6\" in 200\\ image/*) exit 1;; esac
     r7=\$(curl -s -o /dev/null -w '%{http_code}' -m 5 http://127.0.0.1:8080/v1/accounts/account-a); echo \"internal :8080 -> \$r7 (any HTTP code, not 000)\"; [ \"\$r7\" != 000 ]"
}

phase_preflight() {
  o0_record_fleet_state before-caddy
  o0_step "Caddy version (wac-026 🟡-1 f: path cleaning semantics depend on it)" caddy version
  o0_sh "record live sha256 of Caddyfile and v3.env" "sha256sum '$CADDYFILE' '$CADDY_ENV' | tee '$EV/caddy-live.sha256'"
  o0_step "candidate exists and is not the live file" test -s "$CANDIDATE"
  o0_sh "redacted diff live -> candidate (review: only the browser clear/inject lines and the wgw_* block may change)" \
    "diff -u '$CADDYFILE' '$CANDIDATE' | '$O0_PY' '$TOOLS/o0_tool.py' redact | tee '$EV/caddy-diff.redacted.txt'; true"
  o0_step "credential set check incl. control-plane catalog (E-02, E-15; names only)" \
    "$O0_PY" "$TOOLS/o0_watcher_credentials.py" check --watcher-env "$CRED_SET/watcher.env" \
    --holder-env "$CRED_SET/caddy.env" --catalog-env "$OQ_ENV" --require-catalog
  o0_step "caddy validate candidate with env loaded WITHOUT shell expansion (no reload anywhere)" \
    "${ENVX[@]}" caddy validate --adapter caddyfile --config "$CANDIDATE"
  o0_sh "caddy adapt candidate -> 0600 JSON on the host (contains the bcrypt hash: never print)" \
    "umask 077; $(o0_quote "${ENVX[@]}") caddy adapt --adapter caddyfile --config '$CANDIDATE' > '$O0_STAGE_DIR/caddy/candidate.adapted.json'"
  o0_step "verify adapted candidate against the generated path list (anchored, per method, no fallback, browser inject)" \
    "$O0_PY" "$TOOLS/o0_caddy_watcher_routes.py" verify --adapted "$O0_STAGE_DIR/caddy/candidate.adapted.json" \
    --paths "$BUNDLE/caddy/caddy-watcher-gateway-paths.txt" --host "$HOST"
}

rollback_files() {
  o0_step "restore Caddyfile from backup (mode/owner preserved)" cp -p "$BK/Caddyfile.bak" "$CADDYFILE"
  o0_step "restore v3.env from backup (mode/owner preserved)" cp -p "$BK/v3.env.bak" "$CADDY_ENV"
  o0_sh "restored files equal the recorded pre-deploy sha256" "sha256sum -c '$EV/caddy-live.sha256'"
  o0_step "validate restored config with env" "$O0_PY" "$TOOLS/o0_tool.py" env-exec --env-file "$CADDY_ENV" -- caddy validate --adapter caddyfile --config "$CADDYFILE"
  o0_step "restart caddy (restart only; reload is forbidden)" systemctl restart caddy
  o0_step "caddy active" systemctl is-active --quiet caddy
}

phase_apply() {
  o0_note "PRECONDITION: preflight passed in this stage dir; user authorized O0-A05 incl. HALT risk and auto-rollback"
  o0_sh "backup Caddyfile and v3.env into a 0700 dir, mode/owner preserved" \
    "umask 077; mkdir -p '$BK'; chmod 700 '$BK'; cp -p '$CADDYFILE' '$BK/Caddyfile.bak'; cp -p '$CADDY_ENV' '$BK/v3.env.bak'; sha256sum '$BK'/*.bak | tee '$EV/caddy-backup.sha256'"
  if [ "$O0_MODE" = "execute" ]; then
    trap 'o0_log "apply failed: automatic rollback of Caddy files"; rollback_files || o0_log "ROLLBACK FAILED: escalate to user now"; exit 1' ERR
  fi
  o0_step "add WATCHER_BROWSER_PROXY_TOKEN to v3.env (value never printed)" \
    "$O0_PY" "$TOOLS/o0_watcher_credentials.py" apply --fragment "$CRED_SET/caddy.env" --target "$CADDY_ENV" --execute --backup-dir "$BK/env-apply"
  o0_step "install candidate Caddyfile" install -m 0644 -o root -g root "$CANDIDATE" "$CADDYFILE"
  o0_step "validate installed config with env" "$O0_PY" "$TOOLS/o0_tool.py" env-exec --env-file "$CADDY_ENV" -- caddy validate --adapter caddyfile --config "$CADDYFILE"
  o0_step "restart caddy (restart only; reload is forbidden)" systemctl restart caddy
  o0_step "caddy active" systemctl is-active --quiet caddy
  trap - ERR
  o0_record_fleet_state after-caddy
  o0_compare_fleet_state before-caddy after-caddy
}

phase_verify() {
  o0_sh "running config from admin API -> 0600 JSON; must pass the same route verification" \
    "umask 077; curl -s -m 5 http://127.0.0.1:2019/config/ > '$O0_STAGE_DIR/caddy/running.json'"
  o0_step "verify running config" "$O0_PY" "$TOOLS/o0_caddy_watcher_routes.py" verify --adapted "$O0_STAGE_DIR/caddy/running.json" \
    --paths "$BUNDLE/caddy/caddy-watcher-gateway-paths.txt" --host "$HOST"
  probe_public after-caddy
  o0_record_fleet_state verify-caddy
  o0_compare_fleet_state before-caddy verify-caddy
}

phase_rollback() {
  o0_note "rollback = restore the two backed-up files, validate, restart; fleet state recorded; no RESUME"
  rollback_files
  o0_record_fleet_state after-caddy-rollback
  probe_public after-rollback
}

run_phase() {
  case "$1" in
    preflight) phase_preflight ;;
    apply) phase_apply ;;
    verify) phase_verify ;;
    rollback) phase_rollback ;;
    *) o0_die "unknown phase: $1 (preflight|apply|verify|rollback)" ;;
  esac
}
if [ -n "$O0_PHASE" ]; then run_phase "$O0_PHASE"; else for p in preflight apply verify rollback; do o0_note "---- phase $p"; run_phase "$p"; done; fi
