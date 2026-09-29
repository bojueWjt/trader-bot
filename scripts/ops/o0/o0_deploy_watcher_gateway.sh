#!/usr/bin/env bash
# O-0 stage O (WGW-1.0.4 §9.14.6, RS-16; Planner ruling R23): the control-plane role watcher-gateway on 127.0.0.1:8186,
# in its OWN systemd unit, from its OWN code directory ($TRADER_ROOT/releases/watcher-gateway/<release-sha>/), with its
# OWN env file. Replaces the retired o0_deploy_operator_query.sh (it installed into the shared directory and restarted
# operator-query: forbidden in this stage). DRAFT. Runbook: docs/agent-team/release/o0-runbook-deploy.md stage O.
#
# Phases (every phase needs --auth-id O0-A08):
#   preflight  read-only (writes only under the stage dir): shared code dir vs 67b401a (drift = stop with the file list
#              and the user's options, never repaired), NeedDaemonReload=no on EVERY unit, 8186 free (8184-8189 recorded),
#              new objects absent, MainPID/start of the three running units, unit isolation (S-10/RS-17), release tree
#              extracted into staging and verified, credential check, the env built from the whitelist (names only),
#              the dependency check = python -B import smoke with the audit hook (as root: the unit user does not exist
#              yet; the venv and the tree are proven unchanged), unit file lint; writes the wgw-preflight gate
#   apply      gate first; then user, release dir (root 0755), import smoke AS THE UNIT USER, env (0600), unit file,
#              NeedDaemonReload re-check (new unit excluded) + ONE daemon-reload, the three units unchanged,
#              enable --no-reload + start, health, loopback probes, shared dir unchanged, fleet guard.
#              Any failure: automatic rollback of the NEW objects only.
#   verify     O-3: health, loopback probes, 8186 loopback only, four-unit isolation, env names, the three units and the
#              shared dir unchanged, fleet guard; the EXTERNAL V-5 evidence (--v5-evidence) is required: without it
#              DIRECT_GUARD_UNVERIFIED and the verify is NOT complete (exit 1, never a "done" line)
#   rollback   withdraw the exposure FIRST (stop + disable --no-reload, never blocked by NeedDaemonReload), remove env and
#              release dir; then, only if every other unit has NeedDaemonReload=no, remove the unit file, daemon-reload,
#              remove the user; else DAEMON_RELOAD_PENDING (the stopped, disabled unit file stays; report to the user)
#
# NEVER here: write the shared code dir or the shared venv, pip anything, restart/stop/reload node-control, event-ingest
# or operator-query, run jp24-p1-control-plane.sh, daemon-reload while any unit has NeedDaemonReload=yes, RESUME.
set -eo pipefail
# shellcheck source=o0_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/o0_common.sh"
o0_parse_common "$@"
set -- "${O0_REST[@]}"
TRADER_ROOT="/srv/trader-v3" SYSTEMD_DIR="/etc/systemd/system"
CRED_SET="" ACCEPT_DRIFT="" WITH_AUTH_SECRET=0 V5_EVIDENCE="" HOST="jp-bot.balen.wang"
CATALOG_ENVS=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --cred-set) CRED_SET="$2"; shift ;;
    --accept-shared-drift) ACCEPT_DRIFT="$2"; shift ;;          # the user's decision on a MANIFEST_DRIFT list (O0-A08D)
    --with-auth-secret-key) WITH_AUTH_SECRET=1 ;;               # only after U-13 (iii) is confirmed
    --v5-evidence) V5_EVIDENCE="$2"; shift ;;                   # verify: output of the external public-direct-check
    --catalog-env) CATALOG_ENVS+=("$2"); shift ;;
    --host) HOST="$2"; shift ;;
    *) o0_die "unknown argument: $1" ;;
  esac
  shift
done
case "$ACCEPT_DRIFT" in ""|[0-9a-f]*) ;; *) o0_die "--accept-shared-drift takes the drift_sha256 printed by MANIFEST_DRIFT" ;; esac
[ -z "$ACCEPT_DRIFT" ] || [ "${#ACCEPT_DRIFT}" = 64 ] || o0_die "--accept-shared-drift takes a 64-hex drift_sha256"
o0_sandbox_paths TRADER_ROOT SYSTEMD_DIR
UNIT="trader-v3-controlplane-watcher-gateway"
UNIT_FILE="$SYSTEMD_DIR/$UNIT.service"
WUSER="trader-v3-cp-watcher-gateway"
OTHER_UNITS="trader-v3-controlplane-node-control trader-v3-controlplane-event-ingest trader-v3-controlplane-operator-query"
REL_BASE="$TRADER_ROOT/releases/watcher-gateway"
ENV_FILE="$TRADER_ROOT/secrets/control-plane/watcher-gateway.env"
OQ_ENV="$TRADER_ROOT/secrets/control-plane/operator-query.env"
VENV="$TRADER_ROOT/.venv-cp"
WGW_URL="http://127.0.0.1:8186"
HEALTH_TRIES=15                                     # 15 x 2 s = the contract's 30 s
if [ -n "${O0_SANDBOX:-}" ] && [ -n "${O0_WGW_HEALTH_TRIES:-}" ]; then HEALTH_TRIES="$O0_WGW_HEALTH_TRIES"; fi   # local tests only
OQ_URL="http://127.0.0.1:8183"
O0_STAGE_DIR="${O0_STAGE_DIR:-/srv/trader-staging/o0-YYYYMMDDTHHMMSSZ}"
CRED_SET="${CRED_SET:-$O0_STAGE_DIR/creds/set-initial}"
BUNDLE="$O0_STAGE_DIR/bundle"
TOOLS="$BUNDLE/tools"
EV="$O0_STAGE_DIR/evidence"
WB="$BUNDLE/watcher-gateway"
[ "${#CATALOG_ENVS[@]}" -gt 0 ] || CATALOG_ENVS=("$OQ_ENV")
CATALOG_ARGS=()
for f in "${CATALOG_ENVS[@]}"; do CATALOG_ARGS+=(--catalog-env "$f"); done
o0_require_execute_context
REL_SHA="<release-sha>"
if [ -r "$BUNDLE/RELEASE.json" ]; then
  REL_SHA="$("$O0_PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("candidate",""))' "$BUNDLE/RELEASE.json")"
fi
if [ "$O0_MODE" = "execute" ]; then
  [[ "$REL_SHA" =~ ^[0-9a-f]{40}$ ]] || o0_die "RELEASE.json candidate is not a 40-hex commit"
  o0_forbid_patterns "$0" "$(dirname "$0")/o0_common.sh"
  # this script's own red lines (the three running units, pip, the whole-plane script)
  if grep -nE 'systemctl[[:space:]]+(restart|stop|kill|reload|try-restart|reload-or-restart|start|enable|disable)[[:space:]]+(\$OTHER_UNITS|trader-v3-controlplane-(node-control|event-ingest|operator-query))|pip[[:space:]]+install|jp24-p1-control-plane\.sh[[:space:]]+apply' "$0" \
       | grep -v 'o0-allow' | grep -vqE '^[0-9]+:[[:space:]]*#'; then
    o0_die "self-check: this script names a forbidden action"   # o0-allow
  fi
fi
REL_DIR="$REL_BASE/$REL_SHA"
STAGED="$O0_STAGE_DIR/wgw-release/$REL_SHA"
STAGED_ENV="$O0_STAGE_DIR/wgw-env/watcher-gateway.env"
TOOL=("$O0_PY" "$TOOLS/o0_tool.py")
AUTH_ARGS=()
[ "$WITH_AUTH_SECRET" = 1 ] && AUTH_ARGS=(--with-auth-secret-key)

# ---------------------------------------------------------------- steps shared by the phases
units_record() {  # units_record <file>
  o0_sh "record MainPID and start time of the three running control-plane units (they must never change here)" \
    "for u in $OTHER_UNITS; do printf '%s ' \$u; systemctl show \$u -p MainPID,ExecMainStartTimestamp --value | tr '\\n' ' '; echo; done | tee '$EV/$1'"
}
units_unchanged() {  # units_unchanged <before> <after>
  o0_sh "the three running control-plane units are untouched (same MainPID and start time as $1)" \
    "for u in $OTHER_UNITS; do printf '%s ' \$u; systemctl show \$u -p MainPID,ExecMainStartTimestamp --value | tr '\\n' ' '; echo; done > '$EV/$2'
     diff '$EV/$1' '$EV/$2' && echo OTHER_UNITS_UNCHANGED"
}
reload_check() {  # reload_check <label> [unit to exclude]
  o0_sh "NeedDaemonReload=no on EVERY unit ($1; else DAEMON_RELOAD_PENDING: stop, never daemon-reload here)" \
    "systemctl list-units --all --plain --no-legend --no-pager | awk '{print \$1}' | grep -E '\\.[a-z]+\$' | xargs -r systemctl show -p Id -p NeedDaemonReload > '$EV/reload-$1.txt'
     $(o0_quote "${TOOL[@]}") daemon-reload-check --show '$EV/reload-$1.txt' --require trader-v3-controlplane-operator-query.service --require caddy.service ${2:+--exclude $2}"
}
shared_check() {  # preflight: the 67b401a manifest (drift = stop with the list) and the full snapshot
  o0_step "shared code dir vs 67b401a, every tracked file (read-only; drift = stop: file list and the user's options, never repaired)" \
    "${TOOL[@]}" shared-tree --root "$TRADER_ROOT" --manifest "$BUNDLE/cp-shared.baseline.sha256" ${ACCEPT_DRIFT:+--accept-drift-sha256 "$ACCEPT_DRIFT"} \
    --snapshot-out "$EV/cp-shared-preflight.json"
}
shared_same() {  # shared_same <label>
  o0_step "shared code dir byte-identical to the preflight snapshot ($1)" "${TOOL[@]}" shared-tree --root "$TRADER_ROOT" --compare "$EV/cp-shared-preflight.json"
}
port_free() {
  o0_sh "8186 is free; 8184-8189 recorded (PC-6 (ii))" \
    "ss -H -ltnp > '$EV/ss-$1.txt'; $(o0_quote "${TOOL[@]}") port-check --ss-file '$EV/ss-$1.txt' --port 8186 --expect free --record-range 8184-8189"
}
isolation() {  # isolation <may-be-absent 0|1>
  local extra=()
  if [ "$1" = 1 ]; then extra+=(--wgw-may-be-absent); fi
  if [ "$WITH_AUTH_SECRET" = 1 ]; then extra+=(--allow-auth-secret-key); fi
  o0_step "control-plane unit isolation (S-10) of four units (RS-17), names only" \
    "${TOOL[@]}" cp-isolation --oq-unit trader-v3-controlplane-operator-query --other-unit trader-v3-controlplane-node-control \
    --other-unit trader-v3-controlplane-event-ingest --oq-env "$OQ_ENV" --cp-root "$TRADER_ROOT/services/control-plane" \
    --wgw-unit "$UNIT.service" --wgw-env "$ENV_FILE" --wgw-root "$REL_BASE" "${extra[@]}"
}
health() {
  o0_sh "watcher-gateway /health/role -> 200 gateway=enabled app_role=watcher-gateway database=none within 30s" \
    "i=0; until $(o0_quote "${TOOL[@]}") http-probe --url $WGW_URL/health/role --expect-status 200 --expect-json gateway=enabled \\
        --expect-json app_role=watcher-gateway --expect-json database=none --show-json-field gateway; do
       i=\$((i + 1)); [ \$i -lt $HEALTH_TRIES ] || exit 1; sleep 2; done"
}
loopback_probes() {  # the tokens are read in-process from the installed env file; only status codes are printed
  o0_step "8186 /v1/watcher/status with system_observer (read in-process) -> 200 through the watcher" \
    "${TOOL[@]}" http-probe --url "$WGW_URL/v1/watcher/status" --token-env-file "$ENV_FILE" --token-var SYSTEM_OBSERVER_TOKEN --expect-status 200
  o0_step "8186 /v1/watcher/status without a token -> 401 unauthenticated" \
    "${TOOL[@]}" http-probe --url "$WGW_URL/v1/watcher/status" --expect-status 401 --expect-code unauthenticated
  o0_step "8186 /v1/accounts -> 404 (no trading route on watcher-gateway)" "${TOOL[@]}" http-probe --url "$WGW_URL/v1/accounts" --expect-status 404
  o0_step "8183 /v1/watcher/status -> 404 (operator-query has no gateway)" "${TOOL[@]}" http-probe --url "$OQ_URL/v1/watcher/status" --expect-status 404
  o0_sh "8186 listens on 127.0.0.1 only (S-06 re-check)" \
    "ss -H -ltnp > '$EV/ss-$1.txt'; $(o0_quote "${TOOL[@]}") port-check --ss-file '$EV/ss-$1.txt' --port 8186 --expect loopback-only --record-range 8184-8189"
  o0_step "V-5 loopback supplementary check through Caddy (127.0.0.1 with the public name; NOT the external evidence)" \
    "${TOOL[@]}" public-direct-check --host "$HOST" --phase unit-running --loopback
}

# ---------------------------------------------------------------- rollback of the NEW objects only (review wac-096 r5 🟡-B)
withdraw_exposure() {
  o0_sh "withdraw the exposure FIRST: stop the new unit and disable it WITHOUT a reload (never blocked by NeedDaemonReload)" \
    "if [ \"\$(systemctl show -p LoadState --value $UNIT.service)\" = loaded ]; then systemctl stop $UNIT.service || true; fi
     if [ -e '$UNIT_FILE' ]; then systemctl disable --no-reload $UNIT.service || true; fi
     s=\$(systemctl show -p ActiveState --value $UNIT.service); echo \"$UNIT ActiveState=\${s:-<none>}\"; [ \"\$s\" != active ] && [ \"\$s\" != activating ]"
  o0_sh "remove the watcher-gateway env file and the release directory (nothing else)" \
    "rm -f -- '$ENV_FILE'; rm -rf -- '$REL_DIR' '$REL_DIR.staging'; [ ! -e '$ENV_FILE' ] && [ ! -e '$REL_DIR' ] && echo NEW_FILES_REMOVED"
}
remove_unit_file() {
  o0_sh "NeedDaemonReload=no on every other unit, then remove the unit file, daemon-reload once, remove the user; else DAEMON_RELOAD_PENDING (the stopped, disabled unit file stays: report to the user)" \
    "systemctl list-units --all --plain --no-legend --no-pager | awk '{print \$1}' | grep -E '\\.[a-z]+\$' | xargs -r systemctl show -p Id -p NeedDaemonReload > '$EV/reload-rollback.txt'
     if $(o0_quote "${TOOL[@]}") daemon-reload-check --show '$EV/reload-rollback.txt' --require caddy.service --exclude $UNIT.service; then
       if [ -e '$UNIT_FILE' ]; then rm -f -- '$UNIT_FILE'; systemctl daemon-reload; fi
       if getent passwd $WUSER >/dev/null; then userdel $WUSER; fi
       if getent group $WUSER >/dev/null; then groupdel $WUSER; fi
       echo WGW_UNIT_REMOVED
     else
       echo 'DAEMON_RELOAD_PENDING: $UNIT is stopped and disabled; its unit file and user stay until the user authorizes a reload'; exit 1
     fi"
}
wgw_rollback_new() { withdraw_exposure; remove_unit_file; }
wgw_nothing_to_restart() { o0_note "rollback restarts nothing: the three running control-plane units were never touched"; }

# ---------------------------------------------------------------- phases
phase_preflight() {
  o0_gate_clear wgw-preflight
  o0_fleet_baseline preflight-wgw
  o0_bundle_recheck
  o0_sh "bundle: watcher-gateway release tree, its manifest, unit file and env whitelist are present; release sha = candidate" \
    "for f in release.tar release.sha256 $UNIT.service env.whitelist; do [ -f '$WB'/\$f ] || { echo \"missing bundle/watcher-gateway/\$f\"; exit 1; }; done
     [ -f '$BUNDLE/cp-shared.baseline.sha256' ] || { echo 'missing cp-shared.baseline.sha256'; exit 1; }; echo RELEASE_SHA=$REL_SHA"
  reload_check preflight
  shared_check
  port_free preflight
  o0_sh "the new unit file, env file and release dir do not exist yet (the user may: an earlier rollback keeps it only with DAEMON_RELOAD_PENDING)" \
    "[ ! -e '$UNIT_FILE' ] || cmp -s '$UNIT_FILE' '$WB/$UNIT.service' || { echo 'UNIT_FILE_DIFFERS: a different file sits there; stop, report'; exit 1; }
     [ ! -e '$ENV_FILE' ] || { echo 'ENV_FILE_EXISTS: run rollback first'; exit 1; }
     [ ! -e '$REL_DIR' ] || { echo 'RELEASE_DIR_EXISTS: run rollback first'; exit 1; }
     if getent passwd $WUSER >/dev/null; then echo 'NOTE user $WUSER exists (kept by an earlier run)'; fi; echo NEW_OBJECTS_ABSENT"
  units_record cp-units-preflight.txt
  isolation 1
  o0_sh "PC-6 (v): does operator-query.env hold AUTH_SECRET_KEY? (name only; U-13 (iii) decides)" \
    "n=\$(grep -cE '^[[:space:]]*(export[[:space:]]+)?AUTH_SECRET_KEY=' '$OQ_ENV' || true); echo \"PC6_V AUTH_SECRET_KEY_present=\$([ \"\$n\" -gt 0 ] && echo yes || echo no)\""
  o0_sh "extract the release tree into staging and verify every file against the bundle manifest (exact set)" \
    "rm -rf '$STAGED'; mkdir -p '$STAGED'; tar -xf '$WB/release.tar' -C '$STAGED'
     $(o0_quote "${TOOL[@]}") manifest-verify --root '$STAGED' --manifest '$WB/release.sha256' --label wgw-release-staged
     [ \"\$(find '$STAGED' -type f | wc -l | tr -d ' ')\" = \"\$(grep -c . '$WB/release.sha256')\" ] && echo RELEASE_FILE_SET_EXACT"
  o0_step "credential set: the watcher's CURRENT gateway value is what watcher-gateway.env will hold; distinct from the catalog" \
    "$O0_PY" "$TOOLS/o0_watcher_credentials.py" check --watcher-env "$CRED_SET/watcher.env" \
    --holder-env "$CRED_SET/controlplane-watcher-gateway.env" "${CATALOG_ARGS[@]}" --require-catalog
  o0_sh "build the watcher-gateway env in staging from the whitelist (four reader tokens from operator-query.env, the gateway token; names only)" \
    "install -d -m 0700 '$(dirname "$STAGED_ENV")'; rm -f '$STAGED_ENV'
     $(o0_quote "${TOOL[@]}") wgw-env --oq-env '$OQ_ENV' --gateway-fragment '$CRED_SET/controlplane-watcher-gateway.env' --out '$STAGED_ENV' ${AUTH_ARGS[*]}
     $(o0_quote "${TOOL[@]}") wgw-env --check '$STAGED_ENV' ${AUTH_ARGS[*]}"
  o0_step "dependency check: python -B import smoke of the staged tree, whitelisted env, audit hook (as root: the unit user does not exist yet; venv and tree proven unchanged)" \
    "${TOOL[@]}" wgw-smoke --code-dir "$STAGED" --venv "$VENV" --env-file "$STAGED_ENV" --trader-root "$TRADER_ROOT" "${AUTH_ARGS[@]}"
  o0_step "unit file lint (own user, own dir, own env, 127.0.0.1:8186, no database)" \
    "${TOOL[@]}" wgw-unit --lint "$WB/$UNIT.service" --trader-root "$TRADER_ROOT" --release-sha "$REL_SHA"
  o0_gate_write wgw-preflight --field "release_sha=$REL_SHA" --file-sha "staged_env=$STAGED_ENV" --file-sha "live_oq_env=$OQ_ENV" \
    --file-sha "cred_gateway=$CRED_SET/controlplane-watcher-gateway.env" --file-sha "unit_file=$WB/$UNIT.service" --file-sha "release_manifest=$WB/release.sha256" \
    --file-sha "shared_snapshot=$EV/cp-shared-preflight.json"
}

phase_apply() {
  # ---- gates first: nothing below this block writes before all of them pass
  o0_gate_require wgw-preflight "$O0_GATE_MAX_AGE_S" --expect "release_sha=$REL_SHA" --expect-file-sha "staged_env=$STAGED_ENV" \
    --expect-file-sha "live_oq_env=$OQ_ENV" --expect-file-sha "cred_gateway=$CRED_SET/controlplane-watcher-gateway.env" \
    --expect-file-sha "unit_file=$WB/$UNIT.service" --expect-file-sha "release_manifest=$WB/release.sha256" \
    --expect-file-sha "shared_snapshot=$EV/cp-shared-preflight.json"
  o0_bundle_recheck
  reload_check apply-before
  shared_same "before apply"
  port_free apply
  units_unchanged cp-units-preflight.txt cp-units-apply-before.txt
  isolation 1
  o0_fleet_baseline before-wgw
  # ---- changes (new objects only)
  o0_arm_auto_rollback watcher-gateway wgw_rollback_new wgw_nothing_to_restart before-wgw
  o0_sh "create the system user $WUSER (nologin, no home; idempotent)" \
    "getent group $WUSER >/dev/null || groupadd --system $WUSER
     getent passwd $WUSER >/dev/null || useradd --system --gid $WUSER --home-dir /nonexistent --shell /usr/sbin/nologin $WUSER; id $WUSER"
  o0_sh "install the release tree to $REL_DIR (root:root, dirs 0755, files 0644) and verify the exact file set" \
    "install -d -m 0755 -o root -g root '$REL_BASE'; rm -rf '$REL_DIR.staging'; cp -R '$STAGED' '$REL_DIR.staging'
     chown -R root:root '$REL_DIR.staging'; find '$REL_DIR.staging' -type d -exec chmod 0755 {} +; find '$REL_DIR.staging' -type f -exec chmod 0644 {} +
     mv '$REL_DIR.staging' '$REL_DIR'
     $(o0_quote "${TOOL[@]}") manifest-verify --root '$REL_DIR' --manifest '$WB/release.sha256' --label wgw-release-installed
     [ \"\$(find '$REL_DIR' -type f | wc -l | tr -d ' ')\" = \"\$(grep -c . '$WB/release.sha256')\" ] && echo RELEASE_FILE_SET_EXACT"
  o0_step "dependency check AS THE UNIT USER: python -B import smoke of the installed tree (DEPENDENCY_MISSING = automatic rollback; never pip)" \
    "${TOOL[@]}" wgw-smoke --code-dir "$REL_DIR" --venv "$VENV" --env-file "$STAGED_ENV" --trader-root "$TRADER_ROOT" --as-user "$WUSER" "${AUTH_ARGS[@]}"
  o0_sh "install the watcher-gateway env (0600 root; the gated staged file, byte for byte)" \
    "install -m 0600 -o root -g root '$STAGED_ENV' '$ENV_FILE'; cmp -s '$STAGED_ENV' '$ENV_FILE' && echo ENV_INSTALLED names_only"
  o0_sh "install the unit file (0644 root; the bundle's file, byte for byte)" \
    "install -m 0644 -o root -g root '$WB/$UNIT.service' '$UNIT_FILE'; cmp -s '$WB/$UNIT.service' '$UNIT_FILE' && echo UNIT_INSTALLED"
  reload_check apply-before-daemon-reload "$UNIT.service"
  o0_step "daemon-reload (the only one; every other unit had NeedDaemonReload=no)" systemctl daemon-reload
  units_unchanged cp-units-preflight.txt cp-units-after-daemon-reload.txt
  o0_sh "record the unit start time (V-5 evidence must be newer)" "date +%s | tee '$EV/wgw-start.epoch'"
  o0_mark_runtime_replaced
  o0_step "enable watcher-gateway WITHOUT a reload" systemctl enable --no-reload "$UNIT.service"
  o0_step "start watcher-gateway" systemctl start "$UNIT.service"
  health
  loopback_probes apply
  shared_same "after apply"
  units_unchanged cp-units-preflight.txt cp-units-after-apply.txt
  trap - ERR
  o0_fleet_settle_compare before-wgw after-wgw
  o0_note "WGW_APPLY_DONE: not complete until verify (O-3) passes with the EXTERNAL V-5 evidence"
}

phase_verify() {
  health
  loopback_probes verify
  isolation 0
  o0_step "watcher-gateway env: whitelist names only, 0600" "${TOOL[@]}" wgw-env --check "$ENV_FILE" "${AUTH_ARGS[@]}"
  units_unchanged cp-units-preflight.txt cp-units-verify.txt
  shared_same verify
  o0_fleet_settle_compare before-wgw verify-wgw
  o0_sh "V-5 EXTERNAL evidence (outside jp-24, real DNS, no proxy, after the unit start): else DIRECT_GUARD_UNVERIFIED, verify NOT complete" \
    "$(o0_quote "${TOOL[@]}") v5-evidence-check ${V5_EVIDENCE:+--evidence '$V5_EVIDENCE'} --host $HOST --phase unit-running --not-before \"\$(cat '$EV/wgw-start.epoch')\" \\
       || { echo 'WGW_VERIFY_INCOMPLETE DIRECT_GUARD_UNVERIFIED: the rollout is NOT complete'; exit 1; }
     echo WGW_VERIFY_OK"
}

phase_rollback() {
  o0_fleet_record before-wgw-rollback
  withdraw_exposure
  remove_unit_file
  units_unchanged cp-units-preflight.txt cp-units-after-rollback.txt
  shared_same "after rollback"
  o0_fleet_settle_compare before-wgw-rollback after-wgw-rollback
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
