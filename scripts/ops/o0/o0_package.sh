#!/usr/bin/env bash
# O-0 release packaging (LOCAL ONLY; touches no server). DRAFT.
#
# Builds a release bundle for one reviewed candidate commit of
# integ/watcher-app-crew and runs the local release gates:
#   G1 candidate descends from the production baseline (67b401a)
#   G2 contract generator check: ROUTES_DIFF_EMPTY and phase_max=P2   (§9.14.4, wac-016-r2 §7-1, wac-026)
#   G3 the four generated artifacts carry the same yaml_sha256/phase_max; the Caddy list is format v2
#      and the snippet format snippet.v1, the snippet equals the list-derived §9.14.3 shape byte for
#      byte (candidate's o0_caddy_watcher_routes.py check-artifacts; wac-060)          (§9.14.3)
#   G4 watcher require() closure is inside WATCHER_RUNTIME_RELATIVE_PATHS (wac-007 🟡-4)
#   G5 compose passes the six WATCHER_* values to the watcher, healthcheck not interpolated (wac-009 🟡-5, E-16)
#   G6 W-0b merged (lib/config-store.js wired)                                            (wac-009 🟡-7)
#   G7 W-0 integration (wac-015) merged: hand-written watcher-routes.js gone               (wac-007 🟡-2)
#   G8 control-plane change set equals the reviewed file list (no silent extras)
#   G9 (--run-tests) P2 assertion tests + watcher suite                                   (wac-016-r2 §7-1)
#   G10 the candidate's o0 tools and release runbooks contain no forbidden production verb
#       (RESUME, caddy reload/load, /v1/commands, node-control/event-ingest restart, compose down/restart)
#   G11 the candidate commit carries scripts/ops/o0 (the bundle's tools come from the CANDIDATE,
#       never from the packaging worktree) plus the offline image builder
#   G12 the offline builder accepts the bundle's watcher runtime manifest (exact whitelist set,
#       hashes, sizes) - the same validation it runs on jp-24 before building
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
  o0_note "G1/G3/G5-G8/G10-G12 are file checks; G9 runs pytest + npm test when --run-tests"
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
formats = {"contracts/generated/caddy-watcher-gateway-paths.txt": "watcher-gateway-caddy-paths.v2",
           "contracts/generated/caddy-watcher-gateway.caddy": "watcher-gateway-caddy-snippet.v1"}
for rel, fmt in formats.items():
    rows = (root / rel).read_text(encoding="ascii").splitlines()
    head = dict(l[2:].split(" ", 1) for l in rows[:4] if l.startswith("# _"))
    assert set(head) == {"_generated_from", "_yaml_sha256", "_phase_max", "_format"}, (rel, sorted(head))
    assert head["_format"] == fmt, (rel, head["_format"])
    assert head["_phase_max"] == meta["phase_max"] and head["_yaml_sha256"] == meta["yaml_sha256"], (rel, head)
assert meta["phase_max"] == "P2", meta
assert ns["PAYLOAD_SHA256"] in js, "JS artifact does not embed the same payload digest"
print(f"META_OK yaml_sha256={meta['yaml_sha256']} phase_max={meta['phase_max']} payload_sha256={ns['PAYLOAD_SHA256']}")
PY
then
  # the candidate's own tool re-derives the snippet from the list (independent of the generator)
  if (cd "$OUT/src" && "$O0_PY" scripts/ops/o0/o0_caddy_watcher_routes.py check-artifacts \
        --paths contracts/generated/caddy-watcher-gateway-paths.txt --snippet contracts/generated/caddy-watcher-gateway.caddy \
        --expect-phase-max P2 --expect-yaml-sha256 "$(sed -n 's/.* yaml_sha256=\([0-9a-f]*\) .*/\1/p' "$OUT/logs/g3.log")") >>"$OUT/logs/g3.log" 2>&1 \
     && tail -n 1 "$OUT/logs/g3.log" | grep -q '^CADDY_ARTIFACTS_OK '; then
    gate_pass "G3 $(grep '^META_OK' "$OUT/logs/g3.log") $(tail -n 1 "$OUT/logs/g3.log" | cut -d' ' -f1-4)"
  else
    gate_fail "G3 Caddy list/snippet check: $(tail -n 1 "$OUT/logs/g3.log")"
  fi
else gate_fail "G3 generated _meta/_format mismatch (see $OUT/logs/g3.log)"; fi

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
  PYTESTS=(tests/control-plane/test_caddy_watcher_gateway_paths.py tests/control-plane/api/test_watcher_gateway.py)
  [ -f "$OUT/src/tests/deployment/test_watcher_package_contract.py" ] && PYTESTS+=(tests/deployment/test_watcher_package_contract.py)
  if (cd "$OUT/src" && LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 PYTHONUTF8=1 "$MAIN_CHECKOUT/.venv-arch/bin/python" -m pytest -q -p no:cacheprovider \
        "${PYTESTS[@]}") >"$OUT/logs/g9-pytest.log" 2>&1; then
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

# G11 (before G10: G10 scans the candidate's own tools)
CT="$OUT/src/scripts/ops/o0"
TOOL_FILES=()
if [ -d "$CT" ] && [ -f "$CT/o0_common.sh" ] && [ -f "$OUT/src/scripts/build_immutable_watcher_image.py" ] && [ -f "$OUT/src/scripts/release_manifest.py" ]; then
  for f in "$CT"/o0_*.sh "$CT"/o0_*.py "$OUT/src/scripts/sync_operator_risk_db.py" "$OUT/src/scripts/build_immutable_watcher_image.py" "$OUT/src/scripts/release_manifest.py"; do TOOL_FILES+=("$f"); done
  gate_pass "G11 candidate carries scripts/ops/o0 (${#TOOL_FILES[@]} tool files) and the offline image builder"
else
  gate_fail "G11 candidate $CAND_SHA lacks scripts/ops/o0 or the image builder: the bundle's tools must come from the candidate"
fi

# G10
G10_FILES=()
for f in "${TOOL_FILES[@]}"; do case "$f" in */o0_*.sh|*/o0_*.py) G10_FILES+=("$f") ;; esac; done
for f in "$OUT/src/docs/agent-team/release"/o0-*.md; do [ -f "$f" ] && G10_FILES+=("$f"); done
if [ "${#G10_FILES[@]}" -gt 0 ] && (o0_forbid_patterns "${G10_FILES[@]}") >"$OUT/logs/g10.log" 2>&1; then
  gate_pass "G10 ${#G10_FILES[@]} candidate tool/runbook files free of forbidden production verbs"
else
  gate_fail "G10 forbidden production verb or nothing to scan (see $OUT/logs/g10.log)"
fi

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
# Caddy: the COMMITTED list and snippet, copied as is (no render step since wac-060: the candidate
# Caddyfile imports caddy-watcher-gateway.caddy, stage C installs it next to /etc/caddy/Caddyfile)
put "$OUT/src/contracts/generated/caddy-watcher-gateway-paths.txt" "$B/caddy/caddy-watcher-gateway-paths.txt"
put "$OUT/src/contracts/generated/caddy-watcher-gateway.caddy" "$B/caddy/caddy-watcher-gateway.caddy"
for f in "${TOOL_FILES[@]}"; do put "$f" "$B/tools/$(basename "$f")" 0755; done
# G12: runtime manifest for scripts/build_immutable_watcher_image.py (payload = bundle/watcher/<rel>)
if python3 - "$OUT/src/scripts" "$B" "$CAND_SHA" >"$OUT/logs/g12.log" 2>&1 <<'PY'
import hashlib, importlib.util, json, sys
from pathlib import Path
scripts, bundle, cand = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
sys.path.insert(0, str(scripts))
spec = importlib.util.spec_from_file_location("builder", scripts / "build_immutable_watcher_image.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
files = []
for _source, release_path, target_path in builder.WATCHER_RELEASE_FILES:
    path = bundle / release_path
    if not path.is_file():
        raise SystemExit(f"G12 payload missing: {release_path}")
    files.append({"release_path": release_path, "target_path": target_path,
                  "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": path.stat().st_size})
manifest = {"schema_version": builder.WATCHER_RUNTIME_MANIFEST_SCHEMA_VERSION, "files": files,
            "payload_subject_sha256": builder._payload_subject_sha256(files)}
mpath = bundle / builder.WATCHER_RUNTIME_MANIFEST_NAME
mpath.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
source = {"schema": "o0-release-source/v1", "candidate": cand,
          "watcher_runtime": {"manifest": mpath.name, "manifest_sha256": hashlib.sha256(mpath.read_bytes()).hexdigest()}}
(bundle / "release-source-manifest.json").write_text(json.dumps(source, indent=2, sort_keys=True) + "\n", encoding="utf-8")
builder._require_release_source_contract(mpath.resolve())
builder.validate_watcher_runtime_manifest(mpath)
print(f"RUNTIME_MANIFEST_OK files={len(files)} payload_subject_sha256={manifest['payload_subject_sha256'][:16]}")
PY
then gate_pass "G12 $(tail -n 1 "$OUT/logs/g12.log")"; else gate_fail "G12 offline builder rejects the runtime manifest: $(tail -n 1 "$OUT/logs/g12.log")"; fi
cp "$OUT"/manifests/*.sha256 "$B/"
python3 - "$OUT" "$CAND_SHA" "$BASE_SHA" "${#FAILED_GATES[@]}" "$B/tools" > "$B/RELEASE.json" <<'PY'
import hashlib, json, sys
from pathlib import Path
out, cand, base, failed, tools = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), Path(sys.argv[5])
g3 = Path(out, "logs/g3.log").read_text().strip().splitlines()
meta_line = next((l for l in g3 if l.startswith("META_OK")), "")
meta = dict(kv.split("=", 1) for kv in meta_line.split()[1:]) if meta_line else {}
tool_sha = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(tools.glob("*")) if p.is_file()}
print(json.dumps({"schema": "o0-release/v1", "candidate": cand, "baseline": base,
                  "yaml_sha256": meta.get("yaml_sha256"), "phase_max": meta.get("phase_max"),
                  "payload_sha256": meta.get("payload_sha256"), "failed_gates": failed,
                  "tools_from": "candidate:scripts/ops/o0", "tools_sha256": tool_sha,
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
