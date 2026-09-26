#!/usr/bin/env bash
# O-0 stage O: control-plane operator-query (C-0 reader with switch OFF + C-1 gateway). DRAFT.
# Runbook: docs/agent-team/release/o0-runbook-deploy.md stage O.
#
# Phases (every phase needs --auth-id O0-A08):
#   preflight  read-only gates; writes the oq-preflight gate only if all pass
#   apply      refuses unless that gate matches this bundle and inputs; re-runs the cheap
#              gates; then backup, env merge, install, restart operator-query ONLY, inline
#              gateway probe with the system_observer identity (failure = automatic rollback)
#   verify     read-only (reader token read in-process; prints status codes only)
#   rollback   restore files and env, restart operator-query only
#
# Only trader-v3-controlplane-operator-query is restarted. node-control and event-ingest
# are NEVER restarted here (restarting node-control HALTs the whole fleet). They share the
# code directory (decision D-04, to be confirmed by the user), so they will load the new
# read_api.py at their next restart; preflight proves the new code imports cleanly for all
# three roles and registers no /v1/watcher route outside operator-query.
set -eo pipefail
# shellcheck source=o0_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/o0_common.sh"
o0_parse_common "$@"
set -- "${O0_REST[@]}"
CRED_SET="" UNIT="trader-v3-controlplane-operator-query"
OTHER_UNITS="trader-v3-controlplane-node-control trader-v3-controlplane-event-ingest"
CP_ROOT="/srv/trader-v3/services/control-plane"          # confirm with site check S-13
OQ_ENV="/srv/trader-v3/secrets/control-plane/operator-query.env"   # confirm with S-11
VENV_PY="/srv/trader-v3/.venv-cp/bin/python"
OQ_URL="http://127.0.0.1:8183"
HOST="jp-bot.balen.wang"
CATALOG_ENVS=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --cred-set) CRED_SET="$2"; shift ;;
    --cp-root) CP_ROOT="$2"; shift ;;
    --operator-query-env) OQ_ENV="$2"; shift ;;
    --catalog-env) CATALOG_ENVS+=("$2"); shift ;;  # repeat: every control-plane env file from S-10/S-11
    *) o0_die "unknown argument: $1" ;;
  esac
  shift
done
[ "${#CATALOG_ENVS[@]}" -gt 0 ] || CATALOG_ENVS=("$OQ_ENV")
O0_STAGE_DIR="${O0_STAGE_DIR:-/srv/trader-staging/o0-YYYYMMDDTHHMMSSZ}"
CRED_SET="${CRED_SET:-$O0_STAGE_DIR/creds/set-initial}"
BUNDLE="$O0_STAGE_DIR/bundle"
TOOLS="$BUNDLE/tools"
BK="$O0_STAGE_DIR/backup-operator-query"
EV="$O0_STAGE_DIR/evidence"
OVERLAY="$O0_STAGE_DIR/cp-overlay"
PROBE=("$O0_PY" "$TOOLS/o0_tool.py" http-probe)
CATALOG_ARGS=()
for f in "${CATALOG_ENVS[@]}"; do CATALOG_ARGS+=(--catalog-env "$f"); done
o0_require_execute_context
[ "$O0_MODE" != "execute" ] || o0_forbid_patterns "$0" "$(dirname "$0")/o0_common.sh"

venv_check() {
  o0_step "production venv: Python >= 3.11 (asyncio.timeout_at) and httpx importable (wac-016 🟡-8)" \
    "$VENV_PY" -c 'import sys, httpx; assert sys.version_info >= (3, 11), sys.version; print("python", sys.version.split()[0], "httpx", httpx.__version__)'
}
baseline_check() {
  o0_step "live control-plane code equals the 67b401a baseline (abort on hotfix drift; never overwrite unknown code)" \
    "$O0_PY" "$TOOLS/o0_tool.py" manifest-verify --root "$CP_ROOT" --manifest "$BUNDLE/controlplane-context.baseline.sha256" --label cp-live-vs-baseline
}
switch_off_check() {
  o0_sh "snapshot switch is OFF and stays OFF in this stage (C-0 default)" \
    "v=\$(sed -n 's/^WATCHER_CONFIG_SNAPSHOT_ENABLED=//p' '$OQ_ENV'); echo \"WATCHER_CONFIG_SNAPSHOT_ENABLED=\${v:-<unset>}\"; [ -z \"\$v\" ] || [ \"\$v\" = 0 ]"
}
cred_check() {
  o0_step "credential set: operator-query holds the watcher CURRENT gateway/snapshot values; distinct from the catalog" \
    "$O0_PY" "$TOOLS/o0_watcher_credentials.py" check --watcher-env "$CRED_SET/watcher.env" \
    --holder-env "$CRED_SET/operator-query.env" "${CATALOG_ARGS[@]}" --require-catalog
}

phase_preflight() {
  o0_gate_clear oq-preflight
  o0_fleet_baseline preflight-oq
  o0_sh "record MainPID/start time of all three control-plane units (the other two must not change)" \
    "for u in $UNIT $OTHER_UNITS; do printf '%s ' \$u; systemctl show \$u -p MainPID,ExecMainStartTimestamp --value | tr '\\n' ' '; echo; done | tee '$EV/cp-units-before.txt'"
  o0_bundle_recheck
  venv_check
  baseline_check
  switch_off_check
  cred_check
  o0_sh "build an overlay copy (live tree + candidate files) for the import smoke" \
    "rm -rf '$OVERLAY'; mkdir -p '$OVERLAY'; cp -a '$CP_ROOT/.' '$OVERLAY/'; cd '$BUNDLE/controlplane' && find . -type f | sed 's|^\\./||' | while read -r rel; do install -D -m 0644 \"\$rel\" '$OVERLAY'/\"\$rel\"; done"
  o0_sh "import smoke from the overlay with the unit's environment (no network, no DB startup): gateway artifact loads, phase_max=P2, yaml_sha256 as reviewed, /v1/watcher only on operator-query" \
    "cd '$OVERLAY/api' && pp=\$(systemctl show $UNIT -p Environment --value | tr ' ' '\\n' | sed -n 's/^PYTHONPATH=//p' | sed \"s|$CP_ROOT|$OVERLAY|g\")
     PYTHONPATH=\"$OVERLAY/api\${pp:+:\$pp}\" $(o0_quote "$O0_PY" "$TOOLS/o0_tool.py") env-exec --env-file '$OQ_ENV' -- '$VENV_PY' -c 'import json,sys
import read_api, watcher_gateway
assert watcher_gateway._LOAD_ERROR is None, \"gateway artifact failed to load\"
from generated.watcher_gateway_routes import PAYLOAD
rel = json.load(open(sys.argv[1]))
meta = PAYLOAD[\"_meta\"]
assert meta[\"phase_max\"] == \"P2\" and meta[\"yaml_sha256\"] == rel[\"yaml_sha256\"], meta
for role in (\"operator-query\", \"node-control\", \"event-ingest\"):
    app = read_api.create_app(role)
    watcher = [r.path for r in app.routes if getattr(r, \"path\", \"\").startswith(\"/v1/watcher\")]
    assert bool(watcher) == (role == \"operator-query\"), (role, len(watcher))
    print(\"IMPORT_OK\", role, \"watcher_routes=%d\" % len(watcher))' '$BUNDLE/RELEASE.json'"
  o0_gate_write oq-preflight --file-sha "cred_oq_env=$CRED_SET/operator-query.env" --file-sha "live_oq_env=$OQ_ENV"
}

rollback_files() {
  o0_sh "restore the pre-deploy files; delete files the baseline did not have" \
    "tar -C '$CP_ROOT' -xpf '$BK/cp-overwritten.tar'
     while read -r rel; do if [ -n \"\$rel\" ]; then rm -f -- '$CP_ROOT'/\"\$rel\"; fi; done < '$BK/cp-new-files.txt'
     if [ -d '$CP_ROOT/api/generated' ] && [ -z \"\$(ls -A '$CP_ROOT/api/generated')\" ]; then rmdir '$CP_ROOT/api/generated'; fi"
  o0_step "code equals the baseline again" "$O0_PY" "$TOOLS/o0_tool.py" manifest-verify --root "$CP_ROOT" --manifest "$BUNDLE/controlplane.baseline.sha256" --label cp-restored
  o0_step "restore operator-query env file (mode/owner preserved)" cp -p "$BK/operator-query.env.bak" "$OQ_ENV"
  o0_sh "restored env equals the pre-deploy sha" "sha256sum -c '$EV/oq-env-backup.sha256' | sed 's|.*: |operator-query.env backup check: |'"
  o0_step "restart operator-query only" systemctl restart "$UNIT"
  o0_sh "operator-query answers (no token -> 401) within 60s" \
    "i=0; c=000; while [ \$i -lt 30 ]; do c=\$(curl -s -o /dev/null -w '%{http_code}' -m 3 $OQ_URL/v1/accounts || true); [ \"\$c\" = 401 ] && break; sleep 2; i=\$((i + 1)); done; echo \"/v1/accounts -> \$c\"; [ \"\$c\" = 401 ]"
}

phase_apply() {
  # ---- gates first: nothing below this block writes before all of them pass
  o0_gate_require oq-preflight "$O0_GATE_MAX_AGE_S" --expect-file-sha "cred_oq_env=$CRED_SET/operator-query.env" --expect-file-sha "live_oq_env=$OQ_ENV"
  o0_bundle_recheck
  venv_check
  baseline_check
  switch_off_check
  cred_check
  o0_fleet_baseline before-oq
  # ---- changes
  o0_sh "backup overwritten files, list new files, backup env file (0700 dir)" \
    "umask 077; mkdir -p '$BK'; chmod 700 '$BK'
     sed -E 's/^[^ ]+  //' '$BUNDLE/controlplane.candidate.sha256' | while read -r rel; do if [ -e '$CP_ROOT'/\"\$rel\" ]; then echo \"\$rel\"; fi; done > '$BK/cp-overwritten.txt'
     sed -E 's/^[^ ]+  //' '$BUNDLE/controlplane.candidate.sha256' | while read -r rel; do if [ ! -e '$CP_ROOT'/\"\$rel\" ]; then echo \"\$rel\"; fi; done > '$BK/cp-new-files.txt'
     tar -C '$CP_ROOT' -cpf '$BK/cp-overwritten.tar' -T '$BK/cp-overwritten.txt'
     cp -p '$OQ_ENV' '$BK/operator-query.env.bak'; sha256sum '$OQ_ENV' > '$EV/oq-env-backup.sha256'"
  if [ "$O0_MODE" = "execute" ]; then
    trap 'o0_log "apply failed: automatic operator-query rollback"; rollback_files || o0_log "ROLLBACK FAILED: escalate to user now"; exit 1' ERR
  fi
  o0_step "add WATCHER_GATEWAY_TOKEN and WATCHER_SNAPSHOT_TOKEN to the operator-query env (values never printed; switch stays OFF)" \
    "$O0_PY" "$TOOLS/o0_watcher_credentials.py" apply --fragment "$CRED_SET/operator-query.env" --target "$OQ_ENV" --execute --backup-dir "$BK/env-apply"
  o0_step "the live env now holds the watcher CURRENT gateway/snapshot values and still no collision with the catalog" \
    "$O0_PY" "$TOOLS/o0_watcher_credentials.py" check --watcher-env "$CRED_SET/watcher.env" --holder-env "$OQ_ENV" "${CATALOG_ARGS[@]}" --require-catalog
  switch_off_check
  o0_sh "install the five reviewed files (generated/ kept as a package, wac-007 🟡-4)" \
    "cd '$BUNDLE/controlplane' && find . -type f | sed 's|^\\./||' | while read -r rel; do install -D -m 0644 -o root -g root \"\$rel\" '$CP_ROOT'/\"\$rel\"; done"
  o0_step "installed files equal the candidate manifest" \
    "$O0_PY" "$TOOLS/o0_tool.py" manifest-verify --root "$CP_ROOT" --manifest "$BUNDLE/controlplane.candidate.sha256" --label cp-installed
  o0_sh "record the restart time (journal window)" "date +%s | tee '$EV/oq-restart.epoch'"
  o0_step "restart operator-query ONLY (code on disk is not live until restart)" systemctl restart "$UNIT"
  o0_sh "operator-query answers (no token -> 401) within 60s" \
    "i=0; c=000; while [ \$i -lt 30 ]; do c=\$(curl -s -o /dev/null -w '%{http_code}' -m 3 $OQ_URL/v1/accounts || true); [ \"\$c\" = 401 ] && break; sleep 2; i=\$((i + 1)); done; echo \"/v1/accounts -> \$c\"; [ \"\$c\" = 401 ]"
  o0_sh "startup journal since the restart: no artifact-load failure, no gateway token missing/collision" \
    "n=\$(journalctl -u $UNIT --since \"@\$(cat '$EV/oq-restart.epoch')\" --no-pager | awk '/route artifact unavailable|WATCHER_GATEWAY_TOKEN (missing|collision)/{c++} END{print c+0}'); echo \"gateway_startup_error_lines=\$n\"; [ \"\$n\" = 0 ]"
  o0_step "gateway end to end INSIDE the rollback window: system_observer (read in-process) -> 200 through the new watcher (503 gateway_disabled / watcher_unavailable = automatic rollback)" \
    "${PROBE[@]}" --url "$OQ_URL/v1/watcher/status" --token-env-file "$OQ_ENV" --token-var SYSTEM_OBSERVER_TOKEN --expect-status 200
  trap - ERR
  o0_fleet_settle_compare before-oq after-oq
}

phase_verify() {
  o0_step "gateway: no token -> 401 unauthenticated" "${PROBE[@]}" --url "$OQ_URL/v1/watcher/status" --expect-status 401 --expect-code unauthenticated
  o0_step "gateway: bogus token -> 403 invalid_token" "${PROBE[@]}" --url "$OQ_URL/v1/watcher/status" --bogus-bearer --expect-status 403 --expect-code invalid_token
  o0_step "gateway: system_observer token (read in-process) -> 200 through the watcher" \
    "${PROBE[@]}" --url "$OQ_URL/v1/watcher/status" --token-env-file "$OQ_ENV" --token-var SYSTEM_OBSERVER_TOKEN --expect-status 200
  o0_step "gateway: trailing slash -> 404 route_not_found, no redirect" \
    "${PROBE[@]}" --url "$OQ_URL/v1/watcher/status/" --token-env-file "$OQ_ENV" --token-var SYSTEM_OBSERVER_TOKEN --expect-status 404 --expect-code route_not_found --expect-no-location
  o0_step "gateway: P3 path not registered (phase_max=P2) -> 404" \
    "${PROBE[@]}" --url "$OQ_URL/v1/watcher/price-alerts" --token-env-file "$OQ_ENV" --token-var SYSTEM_OBSERVER_TOKEN --expect-status 404
  o0_step "existing endpoint unchanged: /v1/accounts with system_observer -> 200" \
    "${PROBE[@]}" --url "$OQ_URL/v1/accounts" --token-env-file "$OQ_ENV" --token-var SYSTEM_OBSERVER_TOKEN --expect-status 200
  o0_sh "public path through Caddy: /m/v1/watcher/status without token -> 401" \
    "c=\$(curl -s -o /dev/null -w '%{http_code}' -m 10 --resolve $HOST:443:127.0.0.1 https://$HOST/m/v1/watcher/status || true); echo \"-> \$c\"; [ \"\$c\" = 401 ]"
  o0_sh "node-control and event-ingest untouched (same MainPID and start time)" \
    "for u in $UNIT $OTHER_UNITS; do printf '%s ' \$u; systemctl show \$u -p MainPID,ExecMainStartTimestamp --value | tr '\\n' ' '; echo; done > '$EV/cp-units-after.txt'
     diff <(grep -v '^$UNIT ' '$EV/cp-units-before.txt') <(grep -v '^$UNIT ' '$EV/cp-units-after.txt') && echo OTHER_UNITS_UNCHANGED"
  o0_fleet_settle_compare before-oq verify-oq
}

phase_rollback() {
  o0_fleet_record before-oq-rollback
  rollback_files
  o0_fleet_settle_compare before-oq-rollback after-oq-rollback
}

run_phase() {
  case "$1" in
    preflight) phase_preflight ;;
    apply) phase_apply ;;
    verify) phase_verify ;;
    rollback) phase_rollback ;;
    *) o0_die "unknown phase: $1" ;;
  esac
}
if [ -n "$O0_PHASE" ]; then run_phase "$O0_PHASE"; else for p in preflight apply verify rollback; do o0_note "---- phase $p"; run_phase "$p"; done; fi
