#!/usr/bin/env bash
# Offline test of the fleet guard (review wac-032 🔴-1). Runs the REAL o0_common.sh
# functions in execute mode against stubbed `docker` (psql) and `curl` that replay
# fleet samples. Default fixtures are the review's reproduction files
# (scratchpad/fleet/evidence/fleet-{a,b,c,d}.txt); pass O0_FLEET_REPRO_DIR to read them
# from disk instead of the embedded copies.
set -eo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/o0-fleet.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin" "$WORK/stage/evidence" "$WORK/fx"

if [ -n "${O0_FLEET_REPRO_DIR:-}" ]; then
  for n in a b c d; do cp "$O0_FLEET_REPRO_DIR/fleet-$n.txt" "$WORK/fx/repro-$n.txt"; done
else
  : > "$WORK/fx/repro-a.txt"
  : > "$WORK/fx/repro-b.txt"
  echo 'account-a ACTIVE hb_age=1.0' > "$WORK/fx/repro-c.txt"
  echo 'account-a ACTIVE hb_age=412.7' > "$WORK/fx/repro-d.txt"
fi

# The review files use the old 2-column format (no release). The guard query now also
# selects release_id; convert "node status hb_age=x" into "node status <release> hb_age=x".
to_new() { sed -E 's/^([^ ]+) ([^ ]+) (hb_age=.*)$/\1 \2 abcdef012345 \3/' "$1"; }

# stub docker: each call prints the next queued psql sample; stub curl prints 200
cat > "$WORK/bin/docker" <<'STUB'
#!/usr/bin/env bash
n=$(cat "$FLEET_Q/next"); echo $((n + 1)) > "$FLEET_Q/next"
f="$FLEET_Q/sample-$n.txt"; [ -f "$f" ] && cat "$f"; exit 0
STUB
printf '#!/usr/bin/env bash\nprintf 200\n' > "$WORK/bin/curl"
chmod +x "$WORK/bin/docker" "$WORK/bin/curl"

queue() {  # queue <file>...  (sample 0 = baseline, 1.. = after samples)
  rm -rf "$WORK/q"; mkdir -p "$WORK/q"; echo 0 > "$WORK/q/next"
  local i=0 f
  for f in "$@"; do cp "$f" "$WORK/q/sample-$i.txt"; i=$((i + 1)); done
}

run_guard() {  # prints the exit code of: baseline; settle-compare
  ( export PATH="$WORK/bin:$PATH" FLEET_Q="$WORK/q"
    export O0_FLEET_NODES="account-a" O0_FLEET_READY_PORTS="8081" O0_FLEET_SETTLE_S=0 O0_FLEET_INTERVAL_S=0 O0_FLEET_SAMPLES="${SAMPLES:-3}"
    # shellcheck source=../o0_common.sh
    source "$HERE/../o0_common.sh"
    O0_MODE=execute O0_STAGE_DIR="$WORK/stage"
    o0_fleet_baseline before-t 2>/dev/null
    o0_fleet_settle_compare before-t after-t 2>/dev/null
  ) > "$WORK/out.txt" 2>&1 && echo 0 || echo $?
}

fails=0
expect() {  # expect <name> <want-rc> <got-rc>
  if [ "$2" = "$3" ]; then echo "PASS $1 (exit $3)"; else echo "FAIL $1: want exit $2, got $3"; sed 's/^/    /' "$WORK/out.txt"; fails=$((fails + 1)); fi
}

to_new "$WORK/fx/repro-c.txt" > "$WORK/fx/c.txt"
to_new "$WORK/fx/repro-d.txt" > "$WORK/fx/d.txt"
to_new "$WORK/fx/repro-a.txt" > "$WORK/fx/a.txt"
to_new "$WORK/fx/repro-b.txt" > "$WORK/fx/b.txt"
sed 's/ACTIVE/HALTED/' "$WORK/fx/c.txt" > "$WORK/fx/c-halted.txt"
printf 'account-a ACTIVE abcdef012345 hb_age=1.0\naccount-b ACTIVE abcdef012345 hb_age=1.0\n' > "$WORK/fx/extra-node.txt"

queue "$WORK/fx/a.txt" "$WORK/fx/b.txt" "$WORK/fx/b.txt" "$WORK/fx/b.txt"
expect "review repro: empty baseline (0 rows) is UNCOMPARABLE, not unchanged" 2 "$(run_guard)"
grep -q 'FLEET_UNCOMPARABLE' "$WORK/out.txt" || { echo "FAIL missing FLEET_UNCOMPARABLE line"; fails=$((fails + 1)); }

queue "$WORK/fx/c.txt" "$WORK/fx/b.txt" "$WORK/fx/b.txt" "$WORK/fx/b.txt"
expect "0 rows after the change is UNCOMPARABLE" 2 "$(run_guard)"

queue "$WORK/fx/c.txt" "$WORK/fx/d.txt" "$WORK/fx/d.txt" "$WORK/fx/d.txt"
expect "review repro: ACTIVE hb_age 1.0 -> 412.7 (frozen heartbeat) is a CHANGE" 3 "$(run_guard)"
grep -q 'heartbeat frozen' "$WORK/out.txt" || { echo "FAIL frozen heartbeat not named"; fails=$((fails + 1)); }

queue "$WORK/fx/c.txt" "$WORK/fx/c.txt" "$WORK/fx/c.txt" "$WORK/fx/c-halted.txt"
expect "late HALT in the 3rd sample is caught (sampling window, not one immediate sample)" 3 "$(run_guard)"

queue "$WORK/fx/c.txt" "$WORK/fx/c.txt" "$WORK/fx/extra-node.txt" "$WORK/fx/c.txt"
expect "node set changes in a sample -> UNCOMPARABLE" 2 "$(run_guard)"

queue "$WORK/fx/d.txt" "$WORK/fx/d.txt" "$WORK/fx/d.txt" "$WORK/fx/d.txt"
expect "stale baseline (heartbeat already frozen) refuses to start" 2 "$(run_guard)"

queue "$WORK/fx/c.txt" "$WORK/fx/c.txt" "$WORK/fx/c.txt" "$WORK/fx/c.txt"
expect "unchanged, fresh heartbeats in every sample" 0 "$(run_guard)"
grep -q 'FLEET_UNCHANGED_ALL_SAMPLES samples=3' "$WORK/out.txt" || { echo "FAIL all-samples verdict missing"; fails=$((fails + 1)); }

if [ "$fails" -gt 0 ]; then echo "FLEET_GUARD_TEST_FAILED failures=$fails"; exit 1; fi
echo "FLEET_GUARD_TEST_OK cases=7"
