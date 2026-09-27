#!/usr/bin/env bash
# Real Caddy (same version as production, v2.10.2) against the O-0 Caddy tool (wac-060).
# Needs O0_CADDY_BIN=<caddy binary>; without it prints CADDY_REAL_TEST_SKIPPED and exits 0
# (run_all.sh reports the skip on its last line, so a skip is never read as a pass).
#   1. the committed snippet + tests/fixtures/caddy/Caddyfile.prodlike.in: caddyfile-check OK,
#      real `caddy adapt` -> verify OK (the selftest fixture is shaped like this output);
#   2. F-12 / F-13 violations written as real Caddyfiles (12), adapted by real Caddy: verify FAILS;
#      whitelisted top-level directives and non-hitting ones: verify passes (two steps, not
#      "fail on any hit");
#   3. the local probe (`probe`) on the fixture as the "production copy": every §9.14.4 item 1
#      Caddy probe, the fallback probes, F-13 (3) cleaned-and-forwarded dot segments and //,
#      raw-forwarded encoded forms and '#', mobile Authorization kept, browser paths behind
#      basic auth.
# Local only: 127.0.0.1 ports, a temporary directory, a random one-off basic-auth password.
set -eo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
O0="$(cd "$HERE/.." && pwd)"
REPO="$(cd "$O0/../../.." && pwd)"
if [ -z "${O0_CADDY_BIN:-}" ]; then echo "CADDY_REAL_TEST_SKIPPED (O0_CADDY_BIN unset)"; exit 0; fi
CADDY="$O0_CADDY_BIN"
"$CADDY" version | grep -q '^v2\.10\.2 ' || { echo "CADDY_REAL_TEST_FAILED need Caddy v2.10.2 (got $("$CADDY" version | cut -d' ' -f1))"; exit 1; }
WORK="$(mktemp -d "${TMPDIR:-/tmp}/o0-caddy-real.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
T=(python3 "$O0/o0_caddy_watcher_routes.py")
PATHS="$REPO/contracts/generated/caddy-watcher-gateway-paths.txt"
SNIP="$REPO/contracts/generated/caddy-watcher-gateway.caddy"
fails=0; checks=0
ok() { checks=$((checks + 1)); echo "PASS $*"; }
bad() { checks=$((checks + 1)); fails=$((fails + 1)); echo "FAIL $*"; }

cp "$SNIP" "$WORK/caddy-watcher-gateway.caddy"
HASH="$("$CADDY" hash-password --plaintext "$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')")"
sed "s|@@BCRYPT@@|$HASH|" "$HERE/fixtures/caddy/Caddyfile.prodlike.in" > "$WORK/Caddyfile"

adapt_verify() {  # adapt_verify <Caddyfile> -> VRC (verify rc), ARC (adapt rc)
  ARC=0; VRC=0
  "$CADDY" adapt --adapter caddyfile --config "$1" > "$1.json" 2>"$1.err" || ARC=$?
  if [ "$ARC" = 0 ]; then "${T[@]}" verify --adapted "$1.json" --paths "$PATHS" --snippet "$SNIP" > "$1.verify" 2>&1 || VRC=$?; fi
}

# 1. good
if "${T[@]}" caddyfile-check --caddyfile "$WORK/Caddyfile" > "$WORK/cfcheck.txt" 2>&1; then ok "caddyfile-check on the fixture: $(tail -1 "$WORK/cfcheck.txt" | cut -c1-60)"
else bad "caddyfile-check on the fixture"; cat "$WORK/cfcheck.txt"; fi
adapt_verify "$WORK/Caddyfile"
if [ "$ARC" = 0 ] && [ "$VRC" = 0 ]; then ok "real adapt + verify: $(tail -1 "$WORK/Caddyfile.verify")"
else bad "real adapt ($ARC) + verify ($VRC) on the good fixture"; grep -E '^FAIL' "$WORK/Caddyfile.verify" | head -5; tail -3 "$WORK/Caddyfile.err" 2>/dev/null; fi

# 2. variants (python edits the text; each must adapt, then verify must FAIL or PASS as stated)
python3 - "$WORK" <<'PY'
import sys
from pathlib import Path
work = Path(sys.argv[1])
good = (work / "Caddyfile").read_text()
site_import = "\timport watcher_gateway_routes\n"
mobile_handle = "\thandle /m/* {\n\t\turi strip_prefix /m\n\t\treverse_proxy 127.0.0.1:8183\n\t}\n"
def bad(name, text):
    assert text != good, name
    (work / f"bad-{name}.Caddyfile").write_text(text)
def fine(name, text):
    assert text != good, name
    (work / f"ok-{name}.Caddyfile").write_text(text)
bad("handle-before-import", good.replace(site_import, mobile_handle + site_import))
bad("handle-m-after-import", good.replace(site_import, site_import + mobile_handle))
bad("toplevel-rewrite-after-import", good.replace(site_import, site_import + "\trewrite /m/v1/watcher/dialogs /m/v1/watcher/status\n"))
bad("toplevel-request-header", good.replace(site_import, site_import + '\trequest_header Authorization "Bearer {env.SYSTEM_OBSERVER_TOKEN}"\n'))
bad("toplevel-basic-auth", good.replace(site_import, site_import + "\tbasic_auth /m/* {\n\t\tu " + good.split("o0fixture ")[1].split("\n")[0] + "\n\t}\n"))
bad("route-wrapped", good.replace(site_import, "\troute {\n\t\timport watcher_gateway_routes\n\t}\n"))
bad("handle-path-wrapped", good.replace(site_import, "\thandle_path /m* {\n\t\timport watcher_gateway_routes\n\t}\n"))
bad("global-order-reverse-proxy", good.replace("\tadmin localhost:2019\n", "\tadmin localhost:2019\n\torder reverse_proxy before handle\n")
    .replace(site_import, site_import + "\treverse_proxy /m/* 127.0.0.1:8183\n"))
bad("toplevel-redir-prefix", good.replace(site_import, site_import + "\tredir /m/v1/watcher/* /\n"))
bad("toplevel-uri-strip-suffix", good.replace(site_import, site_import + "\turi /m/v1/watcher/* strip_suffix /never-there\n"))
bad("toplevel-method-rewrite", good.replace(site_import, site_import + "\tmethod /m/v1/watcher/* POST\n"))
bad("toplevel-forward-auth", good.replace(site_import, site_import + "\tforward_auth 127.0.0.1:7000 {\n\t\turi /check\n\t}\n"))
fine("whitelisted-toplevel", good.replace(site_import, site_import + "\tvars o0probe 1\n\theader -Server\n\tmap {path} {o0m} {\n\t\tdefault x\n\t}\n"))
fine("redir-non-hitting", good.replace(site_import, site_import + "\tredir /old/* /new/\n\trewrite /static/x /static/y\n"))
PY
for f in "$WORK"/bad-*.Caddyfile; do
  n="$(basename "$f" .Caddyfile)"; adapt_verify "$f"
  if [ "$ARC" != 0 ]; then bad "$n: real caddy adapt failed rc=$ARC ($(tail -1 "$f.err" | cut -c1-120))"
  elif [ "$VRC" != 0 ]; then ok "$n: verify fails ($(grep -m1 -E '^FAIL' "$f.verify" | cut -c1-150))"
  else bad "$n: verify PASSED a violating config"; fi
done
for f in "$WORK"/ok-*.Caddyfile; do
  n="$(basename "$f" .Caddyfile)"; adapt_verify "$f"
  if [ "$ARC" = 0 ] && [ "$VRC" = 0 ]; then ok "$n: verify passes (two-step shadow check)"
  else bad "$n: adapt $ARC verify $VRC"; grep -E '^FAIL' "$f.verify" 2>/dev/null | head -3; fi
done
for n in handle-before-import route-wrapped handle-path-wrapped; do
  if "${T[@]}" caddyfile-check --caddyfile "$WORK/bad-$n.Caddyfile" >/dev/null 2>&1; then bad "caddyfile-check accepted $n"; else ok "caddyfile-check rejects $n (F-12 text rule)"; fi
done
if "${T[@]}" caddyfile-check --caddyfile "$WORK/bad-global-order-reverse-proxy.Caddyfile" | grep -q 'RECORD global line [0-9]*: order reverse_proxy before handle'; then
  ok "caddyfile-check records the global order option for the F-13 (2) manual record"
else bad "global order option not recorded"; fi

# 3. local probe on the fixture as the "production copy"
if "${T[@]}" probe --caddy "$CADDY" --caddyfile "$WORK/Caddyfile" --paths "$PATHS" --snippet "$SNIP" > "$WORK/probe.txt" 2>&1; then
  ok "local probe: $(tail -1 "$WORK/probe.txt")"
else bad "local probe"; grep -E '^FAIL|CADDY_PROBE' "$WORK/probe.txt" | head -12; fi
grep -q '^+http://127.0.0.1:[0-9]* {$' "$WORK/probe.txt" && ! grep -q '\$2a\$' "$WORK/probe.txt" \
  && ok "probe records the line diff (site address rewritten) and prints no bcrypt hash" || bad "probe diff record"

echo "CADDY_REAL_TEST $( [ "$fails" = 0 ] && echo OK || echo FAILED ) checks=$checks failures=$fails caddy=$("$CADDY" version | cut -d' ' -f1)"
[ "$fails" = 0 ]
