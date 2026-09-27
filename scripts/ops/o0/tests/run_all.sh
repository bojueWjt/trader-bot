#!/usr/bin/env bash
# Every offline O-0 check in one go (no network, no production access).
# Optional: O0_WAL_REPRO_DB=<review scratchpad walt/w.db>, O0_FLEET_REPRO_DIR=<review scratchpad fleet/evidence>,
#           O0_CADDY_BIN=<Caddy v2.10.2 binary> (real adapt + local probe, tests/caddy_real_test.sh; the last
#           line says real_caddy=skipped when it is unset, so a skip is never read as a pass)
set -eo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
O0="$HERE/.."
for f in "$O0"/o0_*.sh "$HERE"/*.sh; do bash -n "$f"; done
echo "BASH_N_OK files=$(ls "$O0"/o0_*.sh "$HERE"/*.sh | wc -l | tr -d ' ')"
python3 -m py_compile "$O0"/o0_*.py && echo "PY_COMPILE_OK"
python3 "$O0/o0_tool.py" selftest
python3 "$O0/o0_watcher_credentials.py" selftest | tail -n 1
python3 "$O0/o0_caddy_watcher_routes.py" check-artifacts --expect-phase-max P2
python3 "$O0/o0_caddy_watcher_routes.py" selftest | tail -n 1
python3 "$O0/o0_watcher_config_baseline.py" selftest ${O0_WAL_REPRO_DB:+--repro-db "$O0_WAL_REPRO_DB"}
bash "$HERE/site_check_leak_test.sh"
bash "$HERE/fleet_guard_test.sh" | tail -n 1
bash "$HERE/auth_gate_test.sh" | tail -n 1
bash "$HERE/apply_rollback_test.sh" | tail -n 1
REAL="$(bash "$HERE/caddy_real_test.sh" | tail -n 1)" || true; echo "$REAL"
case "$REAL" in CADDY_REAL_TEST\ OK*) REAL_CADDY=ok ;; CADDY_REAL_TEST_SKIPPED*) REAL_CADDY=skipped ;; *) exit 1 ;; esac
for s in o0_deploy_caddy.sh o0_deploy_watcher.sh o0_deploy_operator_query.sh o0_fleet_guard.sh o0_site_check.sh; do
  bash "$O0/$s" >/dev/null
done
echo "PLAN_MODE_OK scripts=5 (no execution)"
echo "ALL_O0_OFFLINE_CHECKS_OK real_caddy=$REAL_CADDY"
