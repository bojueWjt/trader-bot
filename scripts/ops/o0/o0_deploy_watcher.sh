#!/usr/bin/env bash
# O-0 stage W: telegram-watcher (W-0a + W-0b + W-0 integration). DRAFT.
# Runbook: docs/agent-team/release/o0-runbook-deploy.md stage W.
#
# Phases:
#   preflight   read-only gates (bundle sums, live source == baseline, creds, no session in build context)
#   build       O0-A04: build the candidate image from a STAGED context (live source untouched),
#               require() smoke, consistent DB copy, first-start migration dry-run on the COPY in a
#               --network none container with throwaway fake tokens
#   apply       O0-A07: backups (image tag, source, compose, env_file, DB online copy), install,
#               recreate with the tested image (--no-build), inline verify, automatic rollback on failure
#   verify      read-only post checks
#   rollback    O0-A07: previous image + source + compose; the DB keeps the additive tables
#   restore-db  O0-A07R (separately authorized, destructive for data written after apply)
set -eo pipefail
# shellcheck source=o0_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/o0_common.sh"
o0_parse_common "$@"
set -- "${O0_REST[@]}"
CRED_SET="" WATCHER_ROOT="/srv/trader" PROJECT="trader" SERVICE="watcher" CONTAINER="trader-watcher-1"
COMPOSE_IMAGE="trader-watcher"   # confirm with site check S-05 config_image
WATCHER_DB="/var/lib/docker/volumes/trader_signal-data/_data/watcher-trading.db"
ENV_TARGET="/srv/trader-secrets/watcher-gateway.env"   # must equal the compose env_file path (P-04)
OQ_ENV="/srv/trader-v3/secrets/control-plane/operator-query.env"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --cred-set) CRED_SET="$2"; shift ;;
    --compose-image) COMPOSE_IMAGE="$2"; shift ;;
    --env-target) ENV_TARGET="$2"; shift ;;
    *) o0_die "unknown argument: $1" ;;
  esac
  shift
done
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
[ "$O0_MODE" = "execute" ] && [ -z "$O0_PHASE" ] && o0_die "--phase is required with --execute"
o0_require_execute_context

phase_preflight() {
  o0_record_fleet_state before-watcher
  o0_sh "bundle integrity (SHA256SUMS) and deploy_candidate=true" \
    "cd '$BUNDLE' && sha256sum -c --quiet SHA256SUMS && python3 -c 'import json,sys; r=json.load(open(\"RELEASE.json\")); print(r); sys.exit(0 if r[\"deploy_candidate\"] and r[\"phase_max\"]==\"P2\" else 1)'"
  o0_step "live watcher source equals the reviewed baseline (abort on drift: never overwrite unknown code)" \
    "$O0_PY" "$TOOLS/o0_tool.py" manifest-verify --root "$SRC" --manifest "$BUNDLE/watcher.baseline.sha256" --label watcher-live-vs-baseline
  o0_step "live compose equals the baseline compose" \
    "$O0_PY" "$TOOLS/o0_tool.py" manifest-verify --root "$WATCHER_ROOT" --manifest "$BUNDLE/compose.baseline.sha256" --label compose-live-vs-baseline
  o0_sh "no Telegram session in the build context (it would be baked into the image)" "test ! -e '$SRC/config.json' && echo 'config.json absent'"
  o0_step "credential set: format, pairwise distinct, holders, control-plane catalog (names only)" \
    "$O0_PY" "$TOOLS/o0_watcher_credentials.py" check --watcher-env "$CRED_SET/watcher.env" \
    --holder-env "$CRED_SET/operator-query.env" --holder-env "$CRED_SET/caddy.env" --catalog-env "$OQ_ENV" --require-catalog
  o0_sh "Caddy already injects the browser credential (stage C done; otherwise the site goes 401)" \
    "grep -q '^WATCHER_BROWSER_PROXY_TOKEN=' /etc/caddy/v3.env && curl -s -m 5 http://127.0.0.1:2019/config/ | grep -c 'env.WATCHER_BROWSER_PROXY_TOKEN' >/dev/null && echo CADDY_INJECTS_BROWSER_CREDENTIAL"
  o0_sh "disk headroom on /srv and docker root (build + DB copies)" "df -h /srv \$(docker info -f '{{.DockerRootDir}}') | sed 1d"
}

phase_build() {
  o0_sh "stage a build context mirroring $WATCHER_ROOT (live tree untouched): candidate watcher files over a copy of live source" \
    "umask 022; rm -rf '$O0_STAGE_DIR/build-context'; mkdir -p '$O0_STAGE_DIR/build-context/services' '$O0_STAGE_DIR/build-context/freqtrade'
     mkdir -p '$O0_STAGE_DIR/build-context/services/telegram-watcher'
     tar -C '$SRC' --exclude=./node_modules --exclude=./config.json -cf - . | tar -C '$O0_STAGE_DIR/build-context/services/telegram-watcher' -xf -
     sed -E 's/^[^ ]+  //' '$BUNDLE/watcher.candidate.sha256' | while read -r rel; do rm -f -- '$O0_STAGE_DIR/build-context/services/telegram-watcher'/\"\$rel\"; done
     tar -C '$BUNDLE/watcher' -cf - . | tar -C '$O0_STAGE_DIR/build-context/services/telegram-watcher' -xf -
     tar -C '$WATCHER_ROOT/freqtrade' -cf - signal_strategy | tar -C '$O0_STAGE_DIR/build-context/freqtrade' -xf -
     cp -p '$WATCHER_ROOT'/freqtrade/requirements*.txt '$O0_STAGE_DIR/build-context/freqtrade/'
     test ! -e '$O0_STAGE_DIR/build-context/services/telegram-watcher/config.json'"
  o0_step "candidate files in the staged context match the bundle manifest" \
    "$O0_PY" "$TOOLS/o0_tool.py" manifest-verify --root "$O0_STAGE_DIR/build-context/services/telegram-watcher" --manifest "$BUNDLE/watcher.candidate.sha256" --label staged-context
  o0_step "docker build candidate image (does not touch the running container)" \
    docker build --pull=false -f "$WATCHER_ROOT/docker/watcher/Dockerfile" -t "$CAND_IMAGE" "$O0_STAGE_DIR/build-context"
  o0_step "require() smoke inside the image, no network (wac-007 🟡-4 closure)" \
    docker run --rm --network none --entrypoint node "$CAND_IMAGE" -e \
    "for (const m of ['./lib/auth','./lib/media','./lib/status','./lib/config-store','./lib/trading-api','./lib/generated/gateway-routes']) require(m); console.log('REQUIRE_OK')"
  o0_sh "consistent copy of the LIVE watcher DB (backup API, source read-only) for the migration dry-run" \
    "umask 077; mkdir -p '$DBW'; $(o0_quote "$O0_PY" "$TOOLS/o0_tool.py") sqlite-backup --source '$WATCHER_DB' --dest '$DBW/dryrun.db'"
  o0_sh "pre-migration non-secret baseline of the copy" \
    "$(o0_quote "$O0_PY" "$TOOLS/o0_watcher_config_baseline.py") baseline --db '$DBW/dryrun.db' --export-json '$DBW/dryrun.pre.json' || true"
  o0_sh "throwaway fake credentials for the dry-run container (never the real set)" \
    "umask 077; $(o0_quote "$O0_PY" "$TOOLS/o0_watcher_credentials.py") generate --out-dir '$DBW/fake-creds'
     { cat '$DBW/fake-creds/watcher.env'; printf '%s\n' TRADER_TRADING_DB_PATH=/data/dryrun.db WATCHER_TRADING_DB=/data/dryrun.db TRADING_DB_PATH=/data/dryrun.db PRICE_MONITOR_ENABLED=0 WATCHER_HOST=127.0.0.1 WATCHER_MEDIA_DIR=/data/media; } > '$DBW/dryrun.env'
     echo '{}' > '$DBW/empty-config.json'; chmod 644 '$DBW/empty-config.json'"
  o0_sh "first-start migration dry-run on the COPY: --network none, empty Telegram config, expect 'Web UI listening' and no '[db] Failed' (wac-011-r3 🟡-1)" \
    "docker rm -f o0-watcher-dryrun >/dev/null 2>&1 || true
     docker run -d --name o0-watcher-dryrun --network none -v '$DBW':/data -v '$DBW/empty-config.json':/app/config.json:ro --env-file '$DBW/dryrun.env' '$CAND_IMAGE' >/dev/null
     for i in \$(seq 1 60); do
       if docker logs o0-watcher-dryrun 2>&1 | grep -c 'Web UI listening' >/dev/null; then break; fi
       if [ \"\$(docker inspect -f '{{.State.Running}}' o0-watcher-dryrun)\" != true ]; then break; fi
       sleep 1
     done
     docker logs o0-watcher-dryrun 2>&1 | grep -E '\\[db\\]|Web UI listening|must be configured|conflicting' | tee '$EV/watcher-dryrun.log' || true
     docker rm -f o0-watcher-dryrun >/dev/null
     grep -q 'Web UI listening' '$EV/watcher-dryrun.log' && ! grep -q '\\[db\\] Failed' '$EV/watcher-dryrun.log' && echo MIGRATION_DRYRUN_OK"
  o0_sh "post-migration analysis of the copy: config_revision present, what the first start changed (non-secret diff)" \
    "$(o0_quote "$O0_PY" "$TOOLS/o0_watcher_config_baseline.py") baseline --db '$DBW/dryrun.db' --export-json '$DBW/dryrun.post.json' || true
     python3 -c 'import json,sys; a=json.load(open(sys.argv[1])); b=json.load(open(sys.argv[2]));
print(\"revision_after=\", b[\"revision\"]); assert b[\"revision\"]==0, \"config_revision must start at 0\"
for t in (\"accounts\",\"channels\",\"risks\"):
    print(t, \"UNCHANGED\" if a[\"table_digests\"][t]==b[\"table_digests\"][t] else \"CHANGED_BY_MIGRATION (review export diff)\")' '$DBW/dryrun.pre.json' '$DBW/dryrun.post.json'"
}

rollback_runtime() {
  o0_sh "restore watcher source files (overwritten ones from tar, new ones removed)" \
    "tar -C '$SRC' -xpf '$BK/source-overwritten.tar'
     while read -r rel; do if [ -n \"\$rel\" ]; then rm -f -- '$SRC'/\"\$rel\"; fi; done < '$BK/source-new-files.txt'"
  o0_step "restore compose file" cp -p "$BK/docker-compose.yml.bak" "$COMPOSE"
  o0_sh "live source and compose equal the baseline again" \
    "$(o0_quote "$O0_PY" "$TOOLS/o0_tool.py") manifest-verify --root '$SRC' --manifest '$BUNDLE/watcher.baseline.sha256' && $(o0_quote "$O0_PY" "$TOOLS/o0_tool.py") manifest-verify --root '$WATCHER_ROOT' --manifest '$BUNDLE/compose.baseline.sha256'"
  o0_step "point the compose image name back at the previous image" docker tag "$ROLLBACK_IMAGE" "$COMPOSE_IMAGE"
  o0_step "recreate watcher with the previous image (no build)" "${DC[@]}" up -d --no-deps --force-recreate --no-build "$SERVICE"
  o0_sh "old watcher answers (pre-W-0 code is unauthenticated: /api/status 200)" \
    "for i in \$(seq 1 60); do c=\$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:9090/api/status || true); [ \"\$c\" = 200 ] && break; sleep 2; done; echo \"api/status=\$c\"; [ \"\$c\" = 200 ]"
  o0_note "config_revision/config_audit stay in the DB (additive; old code ignores them). If the snapshot switch is ON, turn it OFF first (wac-011 #6)."
}

phase_apply() {
  o0_note "PRECONDITION: preflight + build passed in this stage dir; stage C (Caddy inject) done; user authorized O0-A07"
  o0_sh "backups: rollback image tag, overwritten/new source list, compose, env_file, DB online copy" \
    "umask 077; mkdir -p '$BK'; chmod 700 '$BK'
     docker tag \"\$(docker inspect -f '{{.Image}}' '$CONTAINER')\" '$ROLLBACK_IMAGE'; docker image inspect -f '{{.Id}}' '$ROLLBACK_IMAGE' | tee '$EV/watcher-rollback-image.txt'
     sed -E 's/^[^ ]+  //' '$BUNDLE/watcher.candidate.sha256' | while read -r rel; do if [ -e '$SRC'/\"\$rel\" ]; then echo \"\$rel\"; fi; done > '$BK/source-overwritten.txt'
     sed -E 's/^[^ ]+  //' '$BUNDLE/watcher.candidate.sha256' | while read -r rel; do if [ ! -e '$SRC'/\"\$rel\" ]; then echo \"\$rel\"; fi; done > '$BK/source-new-files.txt'
     tar -C '$SRC' -cpf '$BK/source-overwritten.tar' -T '$BK/source-overwritten.txt'
     cp -p '$COMPOSE' '$BK/docker-compose.yml.bak'
     if [ -e '$ENV_TARGET' ]; then cp -p '$ENV_TARGET' '$BK/watcher-gateway.env.bak'; fi
     $(o0_quote "$O0_PY" "$TOOLS/o0_tool.py") sqlite-backup --source '$WATCHER_DB' --dest '$BK/watcher-trading.pre-o0.db'"
  if [ "$O0_MODE" = "execute" ]; then
    trap 'o0_log "apply failed: automatic watcher rollback"; rollback_runtime || o0_log "ROLLBACK FAILED: escalate to user now"; exit 1' ERR
  fi
  o0_step "install the watcher env_file (six WATCHER_* names, values never printed)" \
    "$O0_PY" "$TOOLS/o0_watcher_credentials.py" apply --fragment "$CRED_SET/watcher.env" --target "$ENV_TARGET" --create --execute --backup-dir "$BK/env-apply"
  o0_sh "env_file is root 0600" "chown root:root '$ENV_TARGET'; chmod 600 '$ENV_TARGET'; stat -c '%a %U:%G %n' '$ENV_TARGET'"
  o0_sh "install candidate watcher files into the live source tree; remove whitelisted files the candidate no longer ships" \
    "cd '$BUNDLE/watcher' && find . -type f | sed 's|^\\./||' | while read -r rel; do install -D -m 0644 \"\$rel\" '$SRC'/\"\$rel\"; done
     grep '^ABSENT  ' '$BUNDLE/watcher.candidate.sha256' | sed -E 's/^ABSENT  //' | while read -r rel; do rm -f -- '$SRC'/\"\$rel\"; done"
  o0_step "live source now equals the candidate manifest" \
    "$O0_PY" "$TOOLS/o0_tool.py" manifest-verify --root "$SRC" --manifest "$BUNDLE/watcher.candidate.sha256" --label watcher-live-vs-candidate
  o0_step "install candidate compose (env_file wiring, P-04)" install -m 0644 "$BUNDLE/compose/docker-compose.yml" "$COMPOSE"
  o0_step "compose config parses (quiet: never print resolved values)" "${DC[@]}" config --quiet
  o0_step "point the compose image name at the TESTED candidate image" docker tag "$CAND_IMAGE" "$COMPOSE_IMAGE"
  o0_step "recreate watcher with the tested image (no rebuild)" "${DC[@]}" up -d --no-deps --force-recreate --no-build "$SERVICE"
  o0_sh "startup: 'Web UI listening', no '[db] Failed', no credential error, restart count stays 0 for 90s" \
    "for i in \$(seq 1 90); do docker logs --since 5m '$CONTAINER' 2>&1 | grep -c 'Web UI listening' >/dev/null && break; sleep 1; done
     docker logs --since 5m '$CONTAINER' 2>&1 | grep -E '\\[db\\] Failed|must be configured|conflicting trading DB' && exit 1
     docker logs --since 5m '$CONTAINER' 2>&1 | grep -c 'Web UI listening' >/dev/null
     sleep 90; r=\$(docker inspect -f '{{.RestartCount}} {{.State.Running}}' '$CONTAINER'); echo \"restart_count running = \$r\"; [ \"\$r\" = '0 true' ]"
  o0_sh "watcher now refuses unauthenticated calls and accepts the browser identity" \
    "c=\$(curl -s -o /dev/null -w '%{http_code}' -m 5 http://127.0.0.1:9090/api/status); echo \"no credential -> \$c\"; [ \"\$c\" = 401 ]
     $(o0_quote "$O0_PY" "$TOOLS/o0_tool.py") http-probe --url http://127.0.0.1:9090/api/status --token-env-file '$ENV_TARGET' --token-var WATCHER_BROWSER_PROXY_TOKEN --proxy-header X-Watcher-Proxy-Auth --expect-status 200"
  o0_sh "real DB is versioned now (read-only query)" \
    "python3 -c 'import sqlite3,sys; c=sqlite3.connect(\"file:\"+sys.argv[1]+\"?mode=ro\",uri=True); print(\"config_revision\", c.execute(\"SELECT revision FROM config_revision WHERE id=1\").fetchone()); print(\"config_audit rows\", c.execute(\"SELECT count(*) FROM config_audit\").fetchone()[0])' '$WATCHER_DB'"
  trap - ERR
  o0_record_fleet_state after-watcher
  o0_compare_fleet_state before-watcher after-watcher
}

phase_verify() {
  o0_sh "container health becomes healthy (healthcheck carries the browser credential from process.env, E-16)" \
    "for i in \$(seq 1 36); do h=\$(docker inspect -f '{{.State.Health.Status}}' '$CONTAINER'); [ \"\$h\" = healthy ] && break; sleep 5; done; echo \"health=\$h\"; [ \"\$h\" = healthy ]"
  o0_sh "Telegram connection and ingestion resumed (log lines only; no message bodies)" \
    "docker logs --since 10m '$CONTAINER' 2>&1 | grep -ciE 'connected|listening|update' || true"
  o0_sh "watcher env carries no control-plane token names" \
    "docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' '$CONTAINER' | sed 's/=.*//' | grep -E '^(RISK_ADMIN_TOKEN|VIEWER_TOKEN|REVIEWER_TOKEN|SYSTEM_OBSERVER_TOKEN|NAUTILUS_NODE_TOKEN|SIGNAL_TOKEN_ACCOUNT_[A-D]|CONTROL_PLANE_AUTH_SECRET|NAUTILUS_NODE_AUTH_JSON)$' && exit 1 || echo NO_CONTROL_PLANE_TOKENS_IN_WATCHER"
  o0_record_fleet_state verify-watcher
  o0_compare_fleet_state before-watcher verify-watcher
  o0_note "user browser check (basic auth): site loads, dialogs/accounts/risks lists render, images load; report result into evidence"
}

phase_rollback() {
  rollback_runtime
  o0_record_fleet_state after-watcher-rollback
}

phase_restore_db() {
  o0_note "O0-A07R ONLY: replaces the live DB with the pre-apply copy; config writes and Telegram messages stored after apply are LOST"
  o0_note "if the snapshot switch is ON: expect snapshot_revision_regressed or invalid; switch OFF first (runbook-snapshot-switch rollback)"
  o0_step "stop watcher" "${DC[@]}" stop "$SERVICE"
  o0_sh "set aside the current DB files, then install the pre-apply copy" \
    "umask 077; mkdir -p '$BK/db-replaced'; for f in '$WATCHER_DB' '$WATCHER_DB-wal' '$WATCHER_DB-shm'; do [ -e \"\$f\" ] && mv \"\$f\" '$BK/db-replaced/'; done
     cp '$BK/watcher-trading.pre-o0.db' '$WATCHER_DB'; chown root:root '$WATCHER_DB'; chmod 600 '$WATCHER_DB'"
  o0_step "start watcher" "${DC[@]}" start "$SERVICE"
}

run_phase() {
  case "$1" in
    preflight) phase_preflight ;;
    build) phase_build ;;
    apply) phase_apply ;;
    verify) phase_verify ;;
    rollback) phase_rollback ;;
    restore-db) phase_restore_db ;;
    *) o0_die "unknown phase: $1" ;;
  esac
}
if [ -n "$O0_PHASE" ]; then run_phase "$O0_PHASE"; else for p in preflight build apply verify rollback restore-db; do o0_note "---- phase $p"; run_phase "$p"; done; fi
