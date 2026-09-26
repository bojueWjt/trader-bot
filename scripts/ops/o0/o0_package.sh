#!/usr/bin/env bash
# O-0 release packaging (LOCAL ONLY; touches no server). DRAFT.
#
# Builds a release bundle for one reviewed candidate commit of
# integ/watcher-app-crew and runs the local release gates:
#   G1 candidate descends from the production baseline (67b401a)
#   G2 contract generator check: ROUTES_DIFF_EMPTY and phase_max=P2   (§9.14.4, wac-016-r2 §7-1, wac-026)
#   G3 the three generated artifacts carry the same yaml_sha256/phase_max (§9.14.3)
#   G4 watcher require() closure is inside WATCHER_RUNTIME_RELATIVE_PATHS (wac-007 🟡-4)
#   G5 compose passes the six WATCHER_* values to the watcher, healthcheck not interpolated (wac-009 🟡-5, E-16)
#   G6 W-0b merged (lib/config-store.js wired)                                            (wac-009 🟡-7)
#   G7 W-0 integration (wac-015) merged: hand-written watcher-routes.js gone               (wac-007 🟡-2)
#   G8 control-plane change set equals the reviewed file list (no silent extras)
#   G9 (--run-tests) P2 assertion tests + watcher suite                                   (wac-016-r2 §7-1)
#   G10 o0 scripts contain no RESUME / caddy reload
#
# Usage:
#   o0_package.sh --candidate <commit> [--baseline 67b401a] --out <new dir> [--run-tests] [--report-only] [--plan]
# --report-only runs every gate and reports all failures instead of stopping at the first.
set -eo pipefail
# shellcheck source=o0_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/o0_common.sh"

CANDIDATE=""
BASELINE="67b401a"
OUT=""
RUN_TESTS=0
REPORT_ONLY=0
O0_MODE="execute"   # local packaging is safe; --plan still supported
while [ "$#" -gt 0 ]; do
  case "$1" in
    --candidate) CANDIDATE="$2"; shift ;;
    --baseline) BASELINE="$2"; shift ;;
    --out) OUT="$2"; shift ;;
    --run-tests) RUN_TESTS=1 ;;
    --report-only) REPORT_ONLY=1 ;;
    --plan|--dry-run) O0_MODE="plan" ;;
    *) o0_die "unknown argument: $1" ;;
  esac
  shift
done
[ -n "$CANDIDATE" ] || o0_die "--candidate <commit> is required (a reviewed integ/watcher-app-crew commit)"
[ -n "$OUT" ] || o0_die "--out <new directory> is required"
REPO="$(git -C "$O0_SCRIPT_DIR" rev-parse --show-toplevel)"
# .venv-arch and node_modules live in the main checkout, not in task worktrees
MAIN_CHECKOUT="$(dirname "$(git -C "$O0_SCRIPT_DIR" rev-parse --path-format=absolute --git-common-dir)")"

CP_FILES=(
  services/control-plane/api/read_api.py
  services/control-plane/api/watcher_gateway.py
  services/control-plane/api/watcher_config_snapshot.py
  services/control-plane/api/generated/__init__.py
  services/control-plane/api/generated/watcher_gateway_routes.py
)
FAILED_GATES=()
put() { mkdir -p "$(dirname "$2")"; cp "$1" "$2"; chmod "${3:-0644}" "$2"; }
gate_fail() {
  FAILED_GATES+=("$1")
  o0_log "GATE FAIL $1"
  [ "$REPORT_ONLY" = "1" ] || o0_die "gate $1 failed (use --report-only to list every failing gate)"
}
gate_pass() { o0_log "GATE PASS $1"; }

if [ "$O0_MODE" = "plan" ]; then
  o0_note "local packaging plan for candidate=$CANDIDATE baseline=$BASELINE out=$OUT"
  o0_step "export candidate tree" git -C "$REPO" archive --format=tar "$CANDIDATE"
  o0_step "G2 generator check" python3 scripts/contracts/check_watcher_gateway_routes.py
  o0_step "G4 closure" "$O0_PY" "$O0_TOOL" closure --watcher-root bridge/services/telegram-watcher --builder scripts/build_immutable_watcher_image.py
  o0_note "G1/G3/G5-G8/G10 are file checks; G9 runs pytest + npm test when --run-tests"
  exit 0
fi

[ ! -e "$OUT" ] || o0_die "--out must not exist: $OUT"
mkdir -p "$OUT"/{src,base,bundle,manifests,logs}
CAND_SHA="$(git -C "$REPO" rev-parse --verify "$CANDIDATE^{commit}")"
BASE_SHA="$(git -C "$REPO" rev-parse --verify "$BASELINE^{commit}")"
git -C "$REPO" archive --format=tar "$CAND_SHA" | tar -x -C "$OUT/src"
git -C "$REPO" archive --format=tar "$BASE_SHA" -- services/control-plane bridge/services/telegram-watcher bridge/docker-compose.yml | tar -x -C "$OUT/base"
o0_log "candidate=$CAND_SHA baseline=$BASE_SHA"

# G1
if git -C "$REPO" merge-base --is-ancestor "$BASE_SHA" "$CAND_SHA"; then gate_pass G1; else gate_fail "G1 candidate does not descend from baseline"; fi

# G2
if (cd "$OUT/src" && python3 scripts/contracts/check_watcher_gateway_routes.py) >"$OUT/logs/g2.log" 2>&1 \
  && tail -n 1 "$OUT/logs/g2.log" | grep -Eq '^ROUTES_DIFF_EMPTY rows=[1-9][0-9]* yaml_sha256=[0-9a-f]{64} phase_max=P2$'; then
  gate_pass "G2 $(tail -n 1 "$OUT/logs/g2.log")"
else
  gate_fail "G2 generator check (see $OUT/logs/g2.log)"
fi

# G3
if python3 - "$OUT/src" >"$OUT/logs/g3.log" 2>&1 <<'PY'
import json, re, sys
from pathlib import Path
root = Path(sys.argv[1])
ns = {}
exec((root / "services/control-plane/api/generated/watcher_gateway_routes.py").read_text(), ns)
meta = ns["PAYLOAD"]["_meta"]
js = (root / "bridge/services/telegram-watcher/lib/generated/gateway-routes.js").read_text()
caddy = (root / "contracts/generated/caddy-watcher-gateway-paths.txt").read_text().splitlines()
head = dict(l[2:].split(" ", 1) for l in caddy[:3])
assert meta["phase_max"] == "P2", meta
assert head["_phase_max"] == meta["phase_max"] and head["_yaml_sha256"] == meta["yaml_sha256"], head
assert ns["PAYLOAD_SHA256"] in js, "JS artifact does not embed the same payload digest"
print(f"META_OK yaml_sha256={meta['yaml_sha256']} phase_max={meta['phase_max']} payload_sha256={ns['PAYLOAD_SHA256']}")
PY
then gate_pass "G3 $(tail -n 1 "$OUT/logs/g3.log")"; else gate_fail "G3 generated _meta mismatch (see $OUT/logs/g3.log)"; fi

# G4
if "$O0_PY" "$O0_TOOL" closure --watcher-root "$OUT/src/bridge/services/telegram-watcher" \
  --builder "$OUT/src/scripts/build_immutable_watcher_image.py" >"$OUT/logs/g4.log" 2>&1; then
  gate_pass "G4 $(tail -n 1 "$OUT/logs/g4.log")"
else
  gate_fail "G4 watcher closure not whitelisted: $(grep -c CLOSURE_NOT_WHITELISTED "$OUT/logs/g4.log" || true) file(s) (see $OUT/logs/g4.log)"
fi

# G5
if python3 - "$OUT/src/bridge/docker-compose.yml" >"$OUT/logs/g5.log" 2>&1 <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
block = re.search(r"\n  watcher:\n(.*?)(?=\n  [A-Za-z0-9_-]+:\n|\Z)", text, re.S)
assert block, "watcher service block not found"
body = block.group(1)
names = ["WATCHER_GATEWAY_TOKEN", "WATCHER_SNAPSHOT_TOKEN", "WATCHER_BROWSER_PROXY_TOKEN"]
env_file = re.search(r"env_file:", body) is not None
missing = [n for n in names + [n + "_PREVIOUS" for n in names] if n not in body]
assert env_file or not missing, "watcher service passes neither env_file nor all six WATCHER_* names: " + ",".join(missing)
health = re.search(r"healthcheck:(.*?)(?=\n    [a-z_]+:|\Z)", body, re.S)
assert health and not re.search(r"\$\{?WATCHER_", health.group(1)), "healthcheck interpolates WATCHER_* (E-16)"
print("COMPOSE_OK env_file=%s" % env_file)
PY
then gate_pass "G5 $(tail -n 1 "$OUT/logs/g5.log")"; else gate_fail "G5 compose credential wiring: $(tail -n 1 "$OUT/logs/g5.log")"; fi

# G6
W="$OUT/src/bridge/services/telegram-watcher"
if [ -f "$W/lib/config-store.js" ] && grep -q 'require("./config-store")' "$W/lib/trading-api.js"; then gate_pass G6; else gate_fail "G6 W-0b (auto/wac-011) not merged: lib/config-store.js absent"; fi

# G7
if [ ! -e "$W/lib/generated/watcher-routes.js" ] && grep -q 'generated/gateway-routes' "$W/lib/auth.js"; then gate_pass G7; else gate_fail "G7 W-0 integration (wac-015) not merged: hand-written lib/generated/watcher-routes.js still used"; fi

# G8
CHANGED=()
while IFS= read -r line; do [ -n "$line" ] && CHANGED+=("$line"); done < <(git -C "$REPO" diff --name-only "$BASE_SHA" "$CAND_SHA" -- services/control-plane packages | sort)
EXTRA=()
for f in "${CHANGED[@]}"; do
  keep=0
  for c in "${CP_FILES[@]}"; do [ "$f" = "$c" ] && keep=1; done
  [ "$keep" = 1 ] || EXTRA+=("$f")
done
if [ "${#CHANGED[@]}" -gt 0 ] && [ "${#EXTRA[@]}" -eq 0 ]; then gate_pass "G8 control-plane change set = ${#CHANGED[@]} reviewed files"; else gate_fail "G8 unexpected control-plane changes: ${EXTRA[*]:-<none changed>}"; fi

# G9
if [ "$RUN_TESTS" = "1" ]; then
  if (cd "$OUT/src" && LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 PYTHONUTF8=1 "$MAIN_CHECKOUT/.venv-arch/bin/python" -m pytest -q -p no:cacheprovider \
        tests/control-plane/test_caddy_watcher_gateway_paths.py tests/control-plane/api/test_watcher_gateway.py) >"$OUT/logs/g9-pytest.log" 2>&1; then
    gate_pass "G9 pytest $(tail -n 1 "$OUT/logs/g9-pytest.log")"
  else
    gate_fail "G9 pytest (see $OUT/logs/g9-pytest.log)"
  fi
  ln -s "$MAIN_CHECKOUT/bridge/services/telegram-watcher/node_modules" "$W/node_modules"
  # node's summary lines are "# tests N" (TAP) or "ℹ tests N" (spec); 0 tests or any failure/skip = gate failure
  if npm --prefix "$W" test >"$OUT/logs/g9-npm.log" 2>&1 \
    && grep -Eq '^(#|ℹ) fail 0$' "$OUT/logs/g9-npm.log" \
    && grep -Eq '^(#|ℹ) skipped 0$' "$OUT/logs/g9-npm.log" \
    && grep -Eq '^(#|ℹ) tests [1-9][0-9]*$' "$OUT/logs/g9-npm.log"; then
    gate_pass "G9 watcher $(grep -E '^(#|ℹ) (tests|pass|fail|skipped) ' "$OUT/logs/g9-npm.log" | tr '\n' ' ')"
  else
    gate_fail "G9 watcher tests (see $OUT/logs/g9-npm.log)"
  fi
  rm -f "$W/node_modules"
fi

# G10
if o0_forbid_patterns "$O0_SCRIPT_DIR"/o0_*.sh; then gate_pass "G10 o0 scripts free of RESUME and reload verbs"; fi

# ---- bundle + manifests
B="$OUT/bundle"
mkdir -p "$B/controlplane" "$B/watcher" "$B/compose" "$B/caddy" "$B/tools"
printf '%s\n' "${CP_FILES[@]#services/control-plane/}" > "$OUT/manifests/controlplane.paths"
( cd "$OUT/src/services/control-plane" && find api security -maxdepth 2 -name '*.py' -type f | sort ) > "$OUT/manifests/controlplane-context.paths"
( cd "$OUT/base/services/control-plane" && find api security -maxdepth 2 -name '*.py' -type f | sort ) >> "$OUT/manifests/controlplane-context.paths"
sort -u -o "$OUT/manifests/controlplane-context.paths" "$OUT/manifests/controlplane-context.paths"
while IFS= read -r rel; do put "$OUT/src/services/control-plane/$rel" "$B/controlplane/$rel"; done < "$OUT/manifests/controlplane.paths"
"$O0_PY" "$O0_TOOL" manifest-build --root "$B/controlplane" --paths-file "$OUT/manifests/controlplane.paths" --out "$OUT/manifests/controlplane.candidate.sha256"
"$O0_PY" "$O0_TOOL" manifest-build --root "$OUT/base/services/control-plane" --paths-file "$OUT/manifests/controlplane.paths" --out "$OUT/manifests/controlplane.baseline.sha256"
"$O0_PY" "$O0_TOOL" manifest-build --root "$OUT/base/services/control-plane" --paths-file "$OUT/manifests/controlplane-context.paths" --out "$OUT/manifests/controlplane-context.baseline.sha256"

python3 - "$OUT/src/scripts/build_immutable_watcher_image.py" > "$OUT/manifests/watcher.paths" <<'PY'
import ast, sys
tree = ast.parse(open(sys.argv[1], encoding="utf-8").read())
for node in ast.walk(tree):
    if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "WATCHER_RUNTIME_RELATIVE_PATHS" for t in node.targets):
        print("\n".join(sorted(ast.literal_eval(node.value))))
PY
while IFS= read -r rel; do
  if [ -f "$W/$rel" ]; then put "$W/$rel" "$B/watcher/$rel"; fi
done < "$OUT/manifests/watcher.paths"
"$O0_PY" "$O0_TOOL" manifest-build --root "$B/watcher" --paths-file "$OUT/manifests/watcher.paths" --out "$OUT/manifests/watcher.candidate.sha256"
"$O0_PY" "$O0_TOOL" manifest-build --root "$OUT/base/bridge/services/telegram-watcher" --paths-file "$OUT/manifests/watcher.paths" --out "$OUT/manifests/watcher.baseline.sha256"
put "$OUT/src/bridge/docker-compose.yml" "$B/compose/docker-compose.yml"
printf 'docker-compose.yml\n' > "$OUT/manifests/compose.paths"
"$O0_PY" "$O0_TOOL" manifest-build --root "$B/compose" --paths-file "$OUT/manifests/compose.paths" --out "$OUT/manifests/compose.candidate.sha256"
"$O0_PY" "$O0_TOOL" manifest-build --root "$OUT/base/bridge" --paths-file "$OUT/manifests/compose.paths" --out "$OUT/manifests/compose.baseline.sha256"
put "$OUT/src/contracts/generated/caddy-watcher-gateway-paths.txt" "$B/caddy/caddy-watcher-gateway-paths.txt"
"$O0_PY" "$O0_SCRIPT_DIR/o0_caddy_watcher_routes.py" render --paths "$B/caddy/caddy-watcher-gateway-paths.txt" > "$B/caddy/watcher-gateway.caddy"
for f in "$O0_SCRIPT_DIR"/o0_*.sh "$O0_SCRIPT_DIR"/o0_*.py "$OUT/src/scripts/sync_operator_risk_db.py"; do put "$f" "$B/tools/$(basename "$f")" 0755; done
cp "$OUT"/manifests/*.sha256 "$B/"
python3 - "$OUT" "$CAND_SHA" "$BASE_SHA" "${#FAILED_GATES[@]}" > "$B/RELEASE.json" <<'PY'
import json, sys
from pathlib import Path
out, cand, base, failed = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
g3 = Path(out, "logs/g3.log").read_text().strip().splitlines()
meta = dict(kv.split("=", 1) for kv in g3[-1].split()[1:]) if g3 and g3[-1].startswith("META_OK") else {}
print(json.dumps({"schema": "o0-release/v1", "candidate": cand, "baseline": base,
                  "yaml_sha256": meta.get("yaml_sha256"), "phase_max": meta.get("phase_max"),
                  "payload_sha256": meta.get("payload_sha256"), "failed_gates": failed,
                  "deploy_candidate": failed == 0}, indent=1, sort_keys=True))
PY
( cd "$B" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 shasum -a 256 > SHA256SUMS )
tar -C "$OUT" -czf "$OUT/o0-bundle-${CAND_SHA:0:12}.tgz" bundle
shasum -a 256 "$OUT/o0-bundle-${CAND_SHA:0:12}.tgz" | tee "$OUT/o0-bundle-${CAND_SHA:0:12}.tgz.sha256"

if [ "${#FAILED_GATES[@]}" -gt 0 ]; then
  o0_log "PACKAGE_NOT_A_DEPLOY_CANDIDATE failed_gates=${#FAILED_GATES[@]}"
  printf 'FAILED %s\n' "${FAILED_GATES[@]}" >&2
  exit 1
fi
o0_log "PACKAGE_OK candidate=$CAND_SHA bundle=$OUT/o0-bundle-${CAND_SHA:0:12}.tgz"
