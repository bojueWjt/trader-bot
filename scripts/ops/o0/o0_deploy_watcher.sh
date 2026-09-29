#!/usr/bin/env bash
# O-0 stage W: telegram-watcher (W-0a + W-0b + W-0 integration). DRAFT.
# Runbook: docs/agent-team/release/o0-runbook-deploy.md stage W.
#
# Phases and their ONLY accepted authorization (o0_expected_auth):
#   preflight        O0-A04  read-only gates; writes the watcher-preflight gate if all pass
#   build            O0-A04  OFFLINE whitelist build with scripts/build_immutable_watcher_image.py
#                            (running image as the pinned base, reviewed files as an overlay,
#                            docker build --network=none); require() smoke; consistent DB copy;
#                            first-start migration dry-run on the COPY (--network none, fake
#                            tokens, graceful stop); writes the watcher-build gate if all pass
#   apply-preflight  O0-A07  the preflight again, right before apply (gate max age 1 h)
#   apply            O0-A07  refuses unless both gates match this bundle and the candidate
#                            image exists with the gated ID; then backups, install, recreate
#                            with the tested image (--no-build), inline checks incl. Telegram
#                            reconnect, automatic rollback on failure, fleet guard; the automatic
#                            rollback restores files and the image tag, and recreates the
#                            container ONLY if apply had reached the recreate; then fleet guard
#   verify           O0-A07  read-only post checks
#   rollback         O0-A07  previous image + source + compose + env_file state
#   restore-db       O0-A07R + --i-understand-data-loss (destructive for data written after apply)
set -eo pipefail
# shellcheck source=o0_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/o0_common.sh"
o0_parse_common "$@"
set -- "${O0_REST[@]}"
CRED_SET="" WATCHER_ROOT="/srv/trader" PROJECT="trader" SERVICE="watcher" CONTAINER="trader-watcher-1"
COMPOSE_IMAGE="trader-watcher"   # confirm with site check S-05 config_image
WATCHER_DB="/var/lib/docker/volumes/trader_signal-data/_data/watcher-trading.db"
ENV_TARGET="/srv/trader-secrets/watcher-gateway.env"   # must equal the compose env_file path (wac-040)
OQ_ENV="/srv/trader-v3/secrets/control-plane/operator-query.env"
BUILDER_PY="/srv/trader-v3/.venv-cp/bin/python"         # builder needs Python >= 3.11 (tomllib); S-12
TG_TIMEOUT_S=180
CATALOG_ENVS=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --cred-set) CRED_SET="$2"; shift ;;
    --compose-image) COMPOSE_IMAGE="$2"; shift ;;
    --env-target) ENV_TARGET="$2"; shift ;;
    --builder-python) BUILDER_PY="$2"; shift ;;
    --watcher-root) WATCHER_ROOT="$2"; shift ;;   # confirm with site check S-05/S-07 (default /srv/trader)
    --telegram-timeout-s) TG_TIMEOUT_S="$2"; shift ;;
    --catalog-env) CATALOG_ENVS+=("$2"); shift ;;  # repeat: every control-plane env file from S-10/S-11
    *) o0_die "unknown argument: $1" ;;
  esac
  shift
done
o0_sandbox_paths WATCHER_DB OQ_ENV
[ "${#CATALOG_ENVS[@]}" -gt 0 ] || CATALOG_ENVS=("$OQ_ENV")
O0_STAGE_DIR="${O0_STAGE_DIR:-/srv/trader-staging/o0-YYYYMMDDTHHMMSSZ}"
CRED_SET="${CRED_SET:-$O0_STAGE_DIR/creds/set-initial}"
TS="$(basename "$O0_STAGE_DIR" | sed 's/^o0-//')"
BUNDLE="$O0_STAGE_DIR/bundle"
TOOLS="$BUNDLE/tools"
SRC="$WATCHER_ROOT/services/telegram-watcher"
COMPOSE="$WATCHER_ROOT/docker-compose.yml"
CAND_IMAGE="trader-watcher:o0-candidate-$TS"
ROLLBACK_IMAGE="trader-watcher:o0-rollback-$TS"
BK="$O0_STAGE_DIR/backup-watcher"
EV="$O0_STAGE_DIR/evidence"
DBW="$O0_STAGE_DIR/db"
DC=(docker compose --project-name "$PROJECT" --file "$COMPOSE")
TOOL=("$O0_PY" "$TOOLS/o0_tool.py")
BASELINE_TOOL=("$O0_PY" "$TOOLS/o0_watcher_config_baseline.py")
CATALOG_ARGS=()
for f in "${CATALOG_ENVS[@]}"; do CATALOG_ARGS+=(--catalog-env "$f"); done
o0_require_execute_context
[ "$O0_MODE" != "execute" ] || o0_forbid_patterns "$0" "$(dirname "$0")/o0_common.sh"

phase_preflight() {
  o0_gate_clear watcher-preflight
  o0_fleet_baseline preflight-watcher
  o0_bundle_recheck
  o0_step "live watcher source equals the reviewed baseline (abort on drift: never overwrite unknown code)" \
    "${TOOL[@]}" manifest-verify --root "$SRC" --manifest "$BUNDLE/watcher.baseline.sha256" --label watcher-live-vs-baseline
  o0_step "live compose equals the baseline compose" \
    "${TOOL[@]}" manifest-verify --root "$WATCHER_ROOT" --manifest "$BUNDLE/compose.baseline.sha256" --label compose-live-vs-baseline
  o0_sh "no Telegram session in the compose build context (a future 'compose build' would bake it)" "test ! -e '$SRC/config.json' && echo 'config.json absent'"
  o0_step "credential set: format, pairwise distinct, holders, control-plane catalog (names only)" \
    "$O0_PY" "$TOOLS/o0_watcher_credentials.py" check --watcher-env "$CRED_SET/watcher.env" \
    --holder-env "$CRED_SET/controlplane-watcher-gateway.env" --holder-env "$CRED_SET/operator-query.env" --holder-env "$CRED_SET/caddy.env" "${CATALOG_ARGS[@]}" --require-catalog
  o0_sh "Caddy already injects the browser credential (stage C done; otherwise the site goes 401)" \
    "grep -c '^WATCHER_BROWSER_PROXY_TOKEN=' /etc/caddy/v3.env >/dev/null && curl -s -m 5 http://127.0.0.1:2019/config/ | grep -c 'env.WATCHER_BROWSER_PROXY_TOKEN' >/dev/null && echo CADDY_INJECTS_BROWSER_CREDENTIAL"
  o0_sh "disk headroom on /srv and docker root (build + DB copies)" "df -h /srv \$(docker info -f '{{.DockerRootDir}}') | sed 1d"
  o0_gate_write watcher-preflight --file-sha "cred_watcher_env=$CRED_SET/watcher.env" --file-sha "live_compose=$COMPOSE"
}

phase_build() {
  o0_gate_clear watcher-build
  o0_bundle_recheck
  o0_step "builder input: runtime manifest exact-set and hashes (the builder's own validation)" \
    "$BUILDER_PY" "$TOOLS/build_immutable_watcher_image.py" --validate-runtime-manifest "$BUNDLE/watcher-runtime-manifest.json"
  o0_sh "pin the RUNNING image as the base; record its id and org.trader.* labels (lineage)" \
    "umask 077; docker inspect -f '{{.Image}}' '$CONTAINER' | tee '$EV/watcher-base-image.txt'
     docker image inspect -f '{{json .Config.Labels}}' \"\$(cat '$EV/watcher-base-image.txt')\" | $(o0_quote "$O0_PY") -c 'import json,sys; d=json.load(sys.stdin) or {}; [print(k,\"=\",v) for k,v in sorted(d.items()) if k.startswith(\"org.trader\")] or print(\"no org.trader.* labels (base built by compose; lineage = this image id)\")' | tee '$EV/watcher-base-labels.txt'"
  o0_sh "files in the running image that the whitelist does not ship (they stay in the overlay image): list for human review" \
    "docker run --rm --network none --entrypoint find \"\$(cat '$EV/watcher-base-image.txt')\" /app -path /app/node_modules -prune -o -type f -print </dev/null \
       | sed 's|^/app/||' | sort > '$EV/watcher-base-files.txt'
     sed -E 's/^[^ ]+  //' '$BUNDLE/watcher.candidate.sha256' | sort > '$EV/watcher-whitelist.txt'
     comm -23 '$EV/watcher-base-files.txt' '$EV/watcher-whitelist.txt' | tee '$EV/watcher-base-only-files.txt' | sed 's/^/BASE_ONLY /'
     echo \"base_only_files=\$(grep -c . '$EV/watcher-base-only-files.txt' || true) (reviewed by the user before O0-A07; config.json must not be among them)\"
     ! grep -qx 'config.json' '$EV/watcher-base-only-files.txt'"
  o0_sh "OFFLINE whitelist build: FROM the pinned running image, COPY only the reviewed payload, --network=none, --no-cache" \
    "$(o0_quote "$BUILDER_PY" "$TOOLS/build_immutable_watcher_image.py") --manifest '$BUNDLE/watcher-runtime-manifest.json' \
       --base-image \"\$(cat '$EV/watcher-base-image.txt')\" --iid-output '$EV/watcher-candidate.iid' --attestation-output '$EV/watcher-build-attestation.json'
     docker tag \"\$(cat '$EV/watcher-candidate.iid')\" '$CAND_IMAGE'
     docker image inspect -f '{{.Id}}' '$CAND_IMAGE' | tee '$EV/watcher-candidate-image.txt'
     [ \"\$(cat '$EV/watcher-candidate-image.txt')\" = \"\$(cat '$EV/watcher-candidate.iid')\" ]"
  o0_step "require() smoke inside the image, no network (wac-007 🟡-4 closure)" \
    docker run --rm --network none --entrypoint node "$CAND_IMAGE" -e \
    "for (const m of ['./lib/auth','./lib/media','./lib/status','./lib/config-store','./lib/trading-api','./lib/generated/gateway-routes']) require(m); console.log('REQUIRE_OK')"
  o0_sh "consistent copy of the LIVE watcher DB (backup API reads the WAL; source read-only; copy in rollback-journal mode)" \
    "umask 077; rm -rf '$DBW'; mkdir -p '$DBW'; $(o0_quote "${TOOL[@]}") sqlite-backup --source '$WATCHER_DB' --dest '$DBW/dryrun.db'"
  o0_sh "pre-migration non-secret baseline of the copy (exit code recorded; 2 = unreadable = fail)" \
    "set +e; $(o0_quote "${BASELINE_TOOL[@]}") baseline --db '$DBW/dryrun.db' --export-json '$DBW/dryrun.pre.json'; rc=\$?; set -e
     echo \"pre_migration_baseline_rc=\$rc\" | tee '$EV/watcher-dryrun-pre.rc'
     [ \$rc -ne 2 ]"
  o0_sh "throwaway fake credentials and a production-shaped but inert env for the dry-run container" \
    "umask 077; $(o0_quote "$O0_PY" "$TOOLS/o0_watcher_credentials.py") generate --out-dir '$DBW/fake-creds'
     { cat '$DBW/fake-creds/watcher.env'; printf '%s\n' TRADER_TRADING_DB_PATH=/data/dryrun.db WATCHER_TRADING_DB=/data/dryrun.db TRADING_DB_PATH=/data/dryrun.db \
         PRICE_MONITOR_ENABLED=0 HERMES_TRADER_CRON_ENABLED=0 SIGNAL_IMPORTER_ENABLED=0 WATCHER_ALERT_BOT_TOKEN= WATCHER_ALERT_CHAT_ID= \
         WATCHER_HOST=127.0.0.1 WATCHER_MEDIA_DIR=/data/media; } > '$DBW/dryrun.env'
     echo '{}' > '$DBW/empty-config.json'; chmod 644 '$DBW/empty-config.json'"
  o0_sh "first-start migration dry-run on the COPY: --network none, empty Telegram config; wait for 'Web UI listening', then GRACEFUL stop (SIGTERM) so SQLite closes cleanly (wac-011-r3 🟡-1)" \
    "docker rm -f o0-watcher-dryrun >/dev/null 2>&1 || true
     docker run -d --name o0-watcher-dryrun --network none -v '$DBW':/data -v '$DBW/empty-config.json':/app/config.json:ro --env-file '$DBW/dryrun.env' '$CAND_IMAGE' >/dev/null
     i=0; while [ \$i -lt 60 ]; do
       n=\$(docker logs o0-watcher-dryrun 2>&1 | awk '/Web UI listening/{c++} END{print c+0}'); [ \"\$n\" -ge 1 ] && break
       [ \"\$(docker inspect -f '{{.State.Running}}' o0-watcher-dryrun)\" = true ] || break
       sleep 1; i=\$((i + 1))
     done
     docker stop -t 30 o0-watcher-dryrun >/dev/null
     code=\$(docker inspect -f '{{.State.ExitCode}}' o0-watcher-dryrun)
     docker logs o0-watcher-dryrun 2>&1 | awk '/\\[db\\]|Web UI listening|must be configured|conflicting|Shutting down/' > '$EV/watcher-dryrun.log'
     docker rm o0-watcher-dryrun >/dev/null
     cat '$EV/watcher-dryrun.log'; echo \"dryrun_exit_code=\$code\"
     [ \"\$code\" = 0 ] && grep -c 'Web UI listening' '$EV/watcher-dryrun.log' >/dev/null && [ \"\$(awk '/\\[db\\] Failed/{c++} END{print c+0}' '$EV/watcher-dryrun.log')\" = 0 ]
     echo MIGRATION_DRYRUN_OK | tee -a '$EV/watcher-dryrun.log'"
  o0_sh "post-migration copy via the backup API (sees any WAL frames), then analyse THAT copy (never the container's file)" \
    "$(o0_quote "${TOOL[@]}") sqlite-backup --source '$DBW/dryrun.db' --dest '$DBW/dryrun.post.db'
     set +e; $(o0_quote "${BASELINE_TOOL[@]}") baseline --db '$DBW/dryrun.post.db' --export-json '$DBW/dryrun.post.json'; rc=\$?; set -e
     echo \"post_migration_baseline_rc=\$rc\" | tee '$EV/watcher-dryrun-post.rc'
     [ \$rc -ne 2 ]
     $(o0_quote "$O0_PY") -c 'import json,sys; a=json.load(open(sys.argv[1])); b=json.load(open(sys.argv[2]));
print(\"revision_after=\", b[\"revision\"]); assert b[\"revision\"]==0, \"config_revision must exist and start at 0\"
for t in (\"accounts\",\"channels\",\"risks\"):
    print(t, \"UNCHANGED\" if a[\"table_digests\"][t]==b[\"table_digests\"][t] else \"CHANGED_BY_MIGRATION (review export diff)\")' '$DBW/dryrun.pre.json' '$DBW/dryrun.post.json'"
  o0_sh "record the passed build gate (candidate image id bound to this bundle)" \
    "$(o0_quote "${TOOL[@]}") gate-write --out '$(o0_gate_file watcher-build)' --stage watcher-build --bundle '$BUNDLE' \
       --field \"cand_image_id=\$(cat '$EV/watcher-candidate-image.txt')\" --field \"base_image_id=\$(cat '$EV/watcher-base-image.txt')\" \
       --file-sha 'dryrun_log=$EV/watcher-dryrun.log'"
}

rollback_files() {
  o0_sh "restore watcher source files (overwritten ones from tar, new ones removed)" \
    "tar -C '$SRC' -xpf '$BK/source-overwritten.tar'
     while read -r rel; do if [ -n \"\$rel\" ]; then rm -f -- '$SRC'/\"\$rel\"; fi; done < '$BK/source-new-files.txt'"
  o0_step "restore compose file" cp -p "$BK/docker-compose.yml.bak" "$COMPOSE"
  o0_sh "restore the env_file state from before apply (previous file back, or remove the one apply created)" \
    "if [ -e '$BK/watcher-gateway.env.bak' ]; then cp -p '$BK/watcher-gateway.env.bak' '$ENV_TARGET'; echo ENV_FILE_RESTORED
     elif [ -e '$BK/watcher-gateway.env.absent' ]; then rm -f '$ENV_TARGET'; echo ENV_FILE_REMOVED_AS_BEFORE_APPLY
     else echo 'env_file state before apply unknown: left in place (not referenced by the restored compose)'; fi"
  o0_sh "live source and compose equal the baseline again" \
    "$(o0_quote "${TOOL[@]}") manifest-verify --root '$SRC' --manifest '$BUNDLE/watcher.baseline.sha256' && $(o0_quote "${TOOL[@]}") manifest-verify --root '$WATCHER_ROOT' --manifest '$BUNDLE/compose.baseline.sha256'"
  o0_step "point the compose image name back at the previous image (a tag only; the running container is not touched)" docker tag "$ROLLBACK_IMAGE" "$COMPOSE_IMAGE"
}

# only after the running watcher was (or may have been) recreated from the candidate
rollback_runtime() {
  o0_step "recreate watcher with the previous image (no build)" "${DC[@]}" up -d --no-deps --force-recreate --no-build "$SERVICE"
  o0_sh "old watcher answers (pre-W-0 code is unauthenticated: /api/status 200)" \
    "i=0; c=000; while [ \$i -lt 60 ]; do c=\$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:9090/api/status || true); [ \"\$c\" = 200 ] && break; sleep 2; i=\$((i + 1)); done; echo \"api/status=\$c\"; [ \"\$c\" = 200 ]"
  o0_note "config_revision/config_audit stay in the DB (additive; old code ignores them). If the snapshot switch is ON, turn it OFF first (wac-011 #6)."
}

phase_apply() {
  # ---- gates first: nothing below this block writes before all of them pass
  o0_gate_require watcher-preflight "$O0_GATE_MAX_AGE_S" --expect-file-sha "cred_watcher_env=$CRED_SET/watcher.env" --expect-file-sha "live_compose=$COMPOSE"
  o0_sh "the candidate image EXISTS and is the one the build gate tested (checked before anything is stopped)" \
    "id=\$(docker image inspect -f '{{.Id}}' '$CAND_IMAGE') || { echo \"CANDIDATE_IMAGE_MISSING $CAND_IMAGE: run phase build first\"; exit 1; }
     $(o0_quote "${TOOL[@]}") gate-check --gate '$(o0_gate_file watcher-build)' --stage watcher-build --bundle '$BUNDLE' --max-age-s $O0_BUILD_GATE_MAX_AGE_S \
       --expect \"cand_image_id=\$id\" --expect-file-sha 'dryrun_log=$EV/watcher-dryrun.log'
     grep -c '^MIGRATION_DRYRUN_OK$' '$EV/watcher-dryrun.log' >/dev/null"
  o0_bundle_recheck
  o0_step "live watcher source still equals the baseline" \
    "${TOOL[@]}" manifest-verify --root "$SRC" --manifest "$BUNDLE/watcher.baseline.sha256" --label watcher-live-vs-baseline
  o0_step "live compose still equals the baseline" \
    "${TOOL[@]}" manifest-verify --root "$WATCHER_ROOT" --manifest "$BUNDLE/compose.baseline.sha256" --label compose-live-vs-baseline
  o0_step "credential set still valid against the catalog" \
    "$O0_PY" "$TOOLS/o0_watcher_credentials.py" check --watcher-env "$CRED_SET/watcher.env" \
    --holder-env "$CRED_SET/controlplane-watcher-gateway.env" --holder-env "$CRED_SET/operator-query.env" --holder-env "$CRED_SET/caddy.env" "${CATALOG_ARGS[@]}" --require-catalog
  o0_fleet_baseline before-watcher
  # ---- changes
  o0_sh "backups: rollback image tag, overwritten/new source list, compose, env_file state, DB online copy" \
    "umask 077; mkdir -p '$BK'; chmod 700 '$BK'
     docker tag \"\$(docker inspect -f '{{.Image}}' '$CONTAINER')\" '$ROLLBACK_IMAGE'; docker image inspect -f '{{.Id}}' '$ROLLBACK_IMAGE' | tee '$EV/watcher-rollback-image.txt'
     sed -E 's/^[^ ]+  //' '$BUNDLE/watcher.candidate.sha256' | while read -r rel; do if [ -e '$SRC'/\"\$rel\" ]; then echo \"\$rel\"; fi; done > '$BK/source-overwritten.txt'
     sed -E 's/^[^ ]+  //' '$BUNDLE/watcher.candidate.sha256' | while read -r rel; do if [ ! -e '$SRC'/\"\$rel\" ]; then echo \"\$rel\"; fi; done > '$BK/source-new-files.txt'
     tar -C '$SRC' -cpf '$BK/source-overwritten.tar' -T '$BK/source-overwritten.txt'
     cp -p '$COMPOSE' '$BK/docker-compose.yml.bak'
     if [ -e '$ENV_TARGET' ]; then cp -p '$ENV_TARGET' '$BK/watcher-gateway.env.bak'; else : > '$BK/watcher-gateway.env.absent'; fi
     $(o0_quote "${TOOL[@]}") sqlite-backup --source '$WATCHER_DB' --dest '$BK/watcher-trading.pre-o0.db' | tee '$EV/watcher-db-backup.txt'
     sha256sum '$BK/watcher-trading.pre-o0.db' > '$EV/watcher-db-backup.sha256'"
  o0_arm_auto_rollback watcher rollback_files rollback_runtime before-watcher
  o0_step "install the watcher env_file (six WATCHER_* names, values never printed)" \
    "$O0_PY" "$TOOLS/o0_watcher_credentials.py" apply --fragment "$CRED_SET/watcher.env" --target "$ENV_TARGET" --create --execute --backup-dir "$BK/env-apply"
  o0_sh "env_file is root 0600" "chown root:root '$ENV_TARGET'; chmod 600 '$ENV_TARGET'; stat -c '%a %U:%G %n' '$ENV_TARGET'"
  o0_step "installed env_file: format, distinct, and both holders match it (digests only)" \
    "$O0_PY" "$TOOLS/o0_watcher_credentials.py" check --watcher-env "$ENV_TARGET" \
    --holder-env "$CRED_SET/controlplane-watcher-gateway.env" --holder-env "$CRED_SET/operator-query.env" --holder-env "$CRED_SET/caddy.env" "${CATALOG_ARGS[@]}" --require-catalog
  o0_sh "install candidate watcher files into the live source tree; remove whitelisted files the candidate no longer ships" \
    "cd '$BUNDLE/watcher' && find . -type f | sed 's|^\\./||' | while read -r rel; do install -D -m 0644 \"\$rel\" '$SRC'/\"\$rel\"; done
     # awk, not grep: 0 ABSENT lines is the normal case and must not fail the step under pipefail
     awk '/^ABSENT  /{ sub(/^ABSENT  /, \"\"); print }' '$BUNDLE/watcher.candidate.sha256' | while read -r rel; do rm -f -- '$SRC'/\"\$rel\"; done"
  o0_step "live source now equals the candidate manifest" \
    "${TOOL[@]}" manifest-verify --root "$SRC" --manifest "$BUNDLE/watcher.candidate.sha256" --label watcher-live-vs-candidate
  o0_step "install candidate compose (env_file wiring, wac-040)" install -m 0644 "$BUNDLE/compose/docker-compose.yml" "$COMPOSE"
  o0_step "installed compose equals the candidate manifest" \
    "${TOOL[@]}" manifest-verify --root "$WATCHER_ROOT" --manifest "$BUNDLE/compose.candidate.sha256" --label compose-live-vs-candidate
  o0_step "compose config parses (quiet: never print resolved values)" "${DC[@]}" config --quiet
  o0_step "point the compose image name at the TESTED candidate image" docker tag "$CAND_IMAGE" "$COMPOSE_IMAGE"
  o0_sh "record the recreate time (log window for every check below)" "date -u +%Y-%m-%dT%H:%M:%SZ | tee '$EV/watcher-recreate.at'"
  o0_mark_runtime_replaced
  o0_step "recreate watcher with the tested image (no rebuild)" "${DC[@]}" up -d --no-deps --force-recreate --no-build "$SERVICE"
  o0_sh "startup: 'Web UI listening', no '[db] Failed', no credential error, restart count stays 0 for 90s" \
    "since=\$(cat '$EV/watcher-recreate.at'); lg() { docker logs --since \"\$since\" '$CONTAINER' 2>&1; }
     i=0; while [ \$i -lt 90 ]; do [ \"\$(lg | awk '/Web UI listening/{c++} END{print c+0}')\" -ge 1 ] && break; sleep 1; i=\$((i + 1)); done
     bad=\$(lg | awk '/\\[db\\] Failed|must be configured|conflicting trading DB/{c++} END{print c+0}'); echo \"startup_error_lines=\$bad\"; [ \"\$bad\" = 0 ]
     [ \"\$(lg | awk '/Web UI listening/{c++} END{print c+0}')\" -ge 1 ]
     sleep 90; r=\$(docker inspect -f '{{.RestartCount}} {{.State.Running}}' '$CONTAINER'); echo \"restart_count running = \$r\"; [ \"\$r\" = '0 true' ]"
  o0_sh "Telegram collection resumed within ${TG_TIMEOUT_S}s: '[watcher] Connected, listening...' after the recreate; 'Session not authorized' fails at once" \
    "since=\$(cat '$EV/watcher-recreate.at'); lg() { docker logs --since \"\$since\" '$CONTAINER' 2>&1; }
     i=0; ok=0; while [ \$i -lt $TG_TIMEOUT_S ]; do
       [ \"\$(lg | awk '/\\[watcher\\] Session not authorized/{c++} END{print c+0}')\" = 0 ] || { echo TELEGRAM_SESSION_NOT_AUTHORIZED; exit 1; }
       if [ \"\$(lg | awk '/\\[watcher\\] Connected, listening/{c++} END{print c+0}')\" -ge 1 ]; then ok=1; break; fi
       sleep 5; i=\$((i + 5))
     done
     [ \$ok = 1 ] || { echo \"TELEGRAM_NOT_RECONNECTED within ${TG_TIMEOUT_S}s\"; exit 1; }
     echo TELEGRAM_RECONNECTED"
  o0_sh "watcher now refuses unauthenticated calls and accepts the browser identity" \
    "c=\$(curl -s -o /dev/null -w '%{http_code}' -m 5 http://127.0.0.1:9090/api/status || true); echo \"no credential -> \$c\"; [ \"\$c\" = 401 ]
     $(o0_quote "${TOOL[@]}") http-probe --url http://127.0.0.1:9090/api/status --token-env-file '$ENV_TARGET' --token-var WATCHER_BROWSER_PROXY_TOKEN --proxy-header X-Watcher-Proxy-Auth --expect-status 200"
  o0_sh "real DB is versioned now (read-only query)" \
    "$(o0_quote "$O0_PY") -c 'import sqlite3,sys; c=sqlite3.connect(\"file:\"+sys.argv[1]+\"?mode=ro\",uri=True); r=c.execute(\"SELECT revision FROM config_revision WHERE id=1\").fetchone(); print(\"config_revision\", r); print(\"config_audit rows\", c.execute(\"SELECT count(*) FROM config_audit\").fetchone()[0]); sys.exit(0 if r is not None else 1)' '$WATCHER_DB'"
  trap - ERR
  o0_fleet_settle_compare before-watcher after-watcher
}

phase_verify() {
  o0_sh "container health becomes healthy (healthcheck carries the browser credential from process.env, E-16)" \
    "i=0; h=none; while [ \$i -lt 36 ]; do h=\$(docker inspect -f '{{.State.Health.Status}}' '$CONTAINER'); [ \"\$h\" = healthy ] && break; sleep 5; i=\$((i + 1)); done; echo \"health=\$h\"; [ \"\$h\" = healthy ]"
  o0_sh "Telegram: still connected, ingestion counts since the recreate (counts only, no message text; docs/agent-operations.md §1)" \
    "since=\$(cat '$EV/watcher-recreate.at'); lg() { docker logs --since \"\$since\" '$CONTAINER' 2>&1; }
     [ \"\$(lg | awk '/\\[watcher\\] Connected, listening/{c++} END{print c+0}')\" -ge 1 ]
     [ \"\$(docker inspect -f '{{.State.Running}} {{.RestartCount}}' '$CONTAINER')\" = 'true 0' ]
     echo \"forwarded_lines_since_recreate=\$(lg | awk '!/FILTERED|raw-update|debug|^\\[(watcher|db|handler|trading-api|FATAL|auth|media|status)\\]/{c++} END{print c+0}')\" | tee '$EV/watcher-ingest-count.txt'
     echo 'gap check: compare with journalctl -u trader-v3-hermes-feeder --since <recreate time> | grep -c \"Triggered job: signal\" (0 on quiet weekends is not a fault)'"
  o0_sh "watcher env carries no control-plane token names" \
    "n=\$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' '$CONTAINER' | sed 's/=.*//' | awk '/^(RISK_ADMIN_TOKEN|VIEWER_TOKEN|REVIEWER_TOKEN|SYSTEM_OBSERVER_TOKEN|NAUTILUS_NODE_TOKEN|SIGNAL_TOKEN_ACCOUNT_[A-D]|CONTROL_PLANE_AUTH_SECRET|NAUTILUS_NODE_AUTH_JSON)\$/{c++} END{print c+0}'); [ \"\$n\" = 0 ] && echo NO_CONTROL_PLANE_TOKENS_IN_WATCHER"
  o0_fleet_settle_compare before-watcher verify-watcher
  o0_note "user browser check (basic auth): site loads, dialogs/accounts/risks lists render, images load; report result into evidence"
}

phase_rollback() {
  o0_fleet_record before-watcher-rollback
  rollback_files
  rollback_runtime
  o0_fleet_settle_compare before-watcher-rollback after-watcher-rollback
}

# ---- restore-db (O0-A07R). Review wac-032-r2 🟡-5: the file that goes back keeps the
# ORIGINAL owner and mode (recorded with stat before anything moves, not hard-coded), and a
# failure after the watcher was stopped has a recovery path: the automatic rollback puts the
# set-aside files back (mv keeps owner, mode and inode), proves their sha, starts the watcher
# and runs the fleet guard. Manual path: o0-runbook-deploy.md W-6.
DBDIR="$(dirname "$WATCHER_DB")"
DBNAME="$(basename "$WATCHER_DB")"
REPLACED="$BK/db-replaced"
FAILED_RESTORE="$BK/db-failed-restore"

watcher_up_check() {  # watcher_up_check <file with the start time>
  o0_sh "watcher up: 'Web UI listening', no '[db] Failed', Telegram reconnected within ${TG_TIMEOUT_S}s" \
    "since=\$(cat '$1'); lg() { docker logs --since \"\$since\" '$CONTAINER' 2>&1; }
     i=0; while [ \$i -lt $TG_TIMEOUT_S ]; do [ \"\$(lg | awk '/\\[watcher\\] Connected, listening/{c++} END{print c+0}')\" -ge 1 ] && break; sleep 5; i=\$((i + 5)); done
     [ \"\$(lg | awk '/\\[db\\] Failed/{c++} END{print c+0}')\" = 0 ] && [ \"\$(lg | awk '/Web UI listening/{c++} END{print c+0}')\" -ge 1 ] && [ \"\$(lg | awk '/\\[watcher\\] Connected, listening/{c++} END{print c+0}')\" -ge 1 ] && echo RESTORE_DB_WATCHER_UP"
}

# automatic recovery, files: the live DB goes back to the set-aside files (the state before
# restore-db started); whatever the failed restore left behind is kept in db-failed-restore/
restore_db_recover_files() {
  o0_step "recovery: make sure the watcher is stopped before the DB files move" "${DC[@]}" stop "$SERVICE"
  o0_sh "recovery: set the restored copy aside, move the original DB files back (owner, mode, inode kept by mv)" \
    "umask 077; mkdir -p '$FAILED_RESTORE'; chmod 700 '$FAILED_RESTORE'
     while read -r n; do
       if [ -e '$REPLACED'/\"\$n\" ]; then
         if [ -e '$DBDIR'/\"\$n\" ]; then mv -- '$DBDIR'/\"\$n\" '$FAILED_RESTORE'/; fi
         mv -- '$REPLACED'/\"\$n\" '$DBDIR'/\"\$n\"
       fi
     done < '$EV/watcher-db-replaced.files'
     for s in -wal -shm; do
       if [ -e '$DBDIR/$DBNAME'\"\$s\" ] && ! grep -qx '$DBNAME'\"\$s\" '$EV/watcher-db-replaced.files'; then mv -- '$DBDIR/$DBNAME'\"\$s\" '$FAILED_RESTORE'/; fi
     done
     cd '$DBDIR' && sha256sum -c '$EV/watcher-db-replaced.sha256' | sed 's|^|original DB back: |'
     now=\$(stat -c '%u:%g %a' '$WATCHER_DB'); was=\$(cat '$EV/watcher-db-owner.txt'); echo \"owner/mode now=\$now recorded=\$was\"; [ \"\$now\" = \"\$was\" ]
     echo RESTORE_DB_RECOVERED_FILES"
}
# automatic recovery, runtime: the watcher is started again on its original DB
restore_db_recover_runtime() {
  o0_sh "recovery: record the start time" "date -u +%Y-%m-%dT%H:%M:%SZ | tee '$EV/watcher-restore-recovery.at'"
  o0_step "recovery: start watcher on the original DB" "${DC[@]}" start "$SERVICE"
  watcher_up_check "$EV/watcher-restore-recovery.at"
}

phase_restore_db() {
  o0_note "O0-A07R ONLY: replaces the live DB with the pre-apply copy; config writes and Telegram messages stored after apply are LOST"
  o0_sh "snapshot switch must be OFF before the DB goes back in time (otherwise snapshot_revision_regressed / invalid): assert, do not change" \
    "v=\$(sed -n 's/^WATCHER_CONFIG_SNAPSHOT_ENABLED=//p' '$OQ_ENV'); echo \"WATCHER_CONFIG_SNAPSHOT_ENABLED=\${v:-<unset>}\"; [ -z \"\$v\" ] || [ \"\$v\" = 0 ]"
  o0_sh "the pre-apply copy is intact (sha recorded at apply, integrity_check ok)" \
    "sha256sum -c '$EV/watcher-db-backup.sha256'
     $(o0_quote "$O0_PY") -c 'import sqlite3,sys; c=sqlite3.connect(\"file:\"+sys.argv[1]+\"?mode=ro\",uri=True); r=c.execute(\"PRAGMA integrity_check\").fetchone()[0]; print(\"integrity\", r); sys.exit(0 if r==\"ok\" else 1)' '$BK/watcher-trading.pre-o0.db'"
  o0_sh "record owner, mode and sha of the live DB files BEFORE anything moves (the restored file gets the same owner and mode); refuse a second run over earlier set-aside files" \
    "test -f '$WATCHER_DB' || { echo 'LIVE_DB_MISSING $WATCHER_DB'; exit 1; }
     if [ -d '$REPLACED' ] && [ -n \"\$(ls -A '$REPLACED')\" ]; then echo 'DB_REPLACED_NOT_EMPTY: an earlier restore-db set files aside there; recover or move them first (runbook W-6)'; exit 1; fi
     umask 077; mkdir -p '$REPLACED'; chmod 700 '$REPLACED'
     stat -c '%u:%g %a' '$WATCHER_DB' | tee '$EV/watcher-db-owner.txt'
     cd '$DBDIR' && for n in '$DBNAME' '$DBNAME-wal' '$DBNAME-shm'; do if [ -e \"\$n\" ]; then echo \"\$n\"; fi; done > '$EV/watcher-db-replaced.files'
     xargs sha256sum < '$EV/watcher-db-replaced.files' > '$EV/watcher-db-replaced.sha256'
     sed 's|^|set aside: |' '$EV/watcher-db-replaced.files'"
  o0_fleet_baseline before-restore-db
  o0_arm_auto_rollback watcher-restore-db restore_db_recover_files restore_db_recover_runtime before-restore-db
  o0_mark_runtime_replaced
  o0_step "stop watcher" "${DC[@]}" stop "$SERVICE"
  o0_sh "set aside the current DB files, then install the pre-apply copy with the RECORDED owner and mode and prove bytes, owner and mode" \
    "while read -r n; do mv -- '$DBDIR'/\"\$n\" '$REPLACED'/; done < '$EV/watcher-db-replaced.files'
     own=\$(cut -d' ' -f1 '$EV/watcher-db-owner.txt'); mode=\$(cut -d' ' -f2 '$EV/watcher-db-owner.txt')
     cp -- '$BK/watcher-trading.pre-o0.db' '$WATCHER_DB'; chown \"\$own\" '$WATCHER_DB'; chmod \"\$mode\" '$WATCHER_DB'
     [ \"\$(sha256sum < '$WATCHER_DB' | cut -d' ' -f1)\" = \"\$(cut -d' ' -f1 '$EV/watcher-db-backup.sha256')\" ] || { echo RESTORED_DB_SHA_MISMATCH; exit 1; }
     echo RESTORED_DB_SHA_OK
     now=\$(stat -c '%u:%g %a' '$WATCHER_DB'); echo \"owner/mode now=\$now recorded=\$(cat '$EV/watcher-db-owner.txt')\"
     [ \"\$now\" = \"\$(cat '$EV/watcher-db-owner.txt')\" ] || { echo RESTORED_DB_OWNER_MODE_MISMATCH; exit 1; }
     echo RESTORED_DB_OWNER_MODE_OK"
  o0_sh "record the restart time" "date -u +%Y-%m-%dT%H:%M:%SZ | tee '$EV/watcher-restore.at'"
  o0_step "start watcher" "${DC[@]}" start "$SERVICE"
  watcher_up_check "$EV/watcher-restore.at"
  trap - ERR
  o0_fleet_settle_compare before-restore-db after-restore-db
}

run_phase() {
  case "$1" in
    preflight|apply-preflight) phase_preflight ;;
    build) phase_build ;;
    apply) phase_apply ;;
    verify) phase_verify ;;
    rollback) phase_rollback ;;
    restore-db) phase_restore_db ;;
    *) o0_die "unknown phase: $1" ;;
  esac
}
if [ -n "$O0_PHASE" ]; then run_phase "$O0_PHASE"; else for p in preflight build apply-preflight apply verify rollback restore-db; do o0_note "---- phase $p"; run_phase "$p"; done; fi
