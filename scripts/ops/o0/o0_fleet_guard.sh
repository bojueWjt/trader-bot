#!/usr/bin/env bash
# O-0 fleet guard for the hand-run runbook steps (credential rotation R-x, snapshot switch SW-x).
# DRAFT. Read-only: it only samples the fleet (docs/agent-operations.md §0) and compares.
#
#   before the step:  o0_fleet_guard.sh --execute --phase <step> --auth-id <that step's id> --stage-dir $S --action before
#   after the step:   o0_fleet_guard.sh --execute --phase <step> --auth-id <that step's id> --stage-dir $S --action after
#   (rollback of a step: --action record, then --action after; recording never blocks a rollback)
#
# `before` refuses (exit 2) when the node set is not exactly O0_FLEET_NODES or any heartbeat is
# older than 5 s: do not start the step. `after` waits the settle window and samples several
# times; any status/release/ready change or a frozen heartbeat = exit 3: stop, report to the
# user, never RESUME. The step ids and their authorization are in o0_common.sh o0_expected_auth.
set -eo pipefail
# shellcheck source=o0_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/o0_common.sh"
o0_parse_common "$@"
set -- "${O0_REST[@]}"
ACTION=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --action) ACTION="$2"; shift ;;
    *) o0_die "unknown argument: $1" ;;
  esac
  shift
done
O0_STAGE_DIR="${O0_STAGE_DIR:-/srv/trader-staging/o0-YYYYMMDDTHHMMSSZ}"
O0_PHASE="${O0_PHASE:-R-2}"
o0_require_execute_context
case "$ACTION" in
  before) o0_fleet_baseline "$O0_PHASE-before" ;;
  record) o0_fleet_record "$O0_PHASE-before" ;;
  after) o0_fleet_settle_compare "$O0_PHASE-before" "$O0_PHASE-after" ;;
  "") o0_note "plan: --action before | record | after"; o0_fleet_baseline "$O0_PHASE-before"; o0_fleet_settle_compare "$O0_PHASE-before" "$O0_PHASE-after" ;;
  *) o0_die "unknown --action $ACTION (before|record|after)" ;;
esac
