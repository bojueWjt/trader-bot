#!/usr/bin/env bash
# Real Caddy (same version as production, v2.10.2) against the O-0 Caddy tool (wac-060).
# Needs O0_CADDY_BIN=<caddy binary>; without it prints CADDY_REAL_TEST_SKIPPED and exits 0
# (run_all.sh reports the skip on its last line, so a skip is never read as a pass).
#   1. the committed snippet + tests/fixtures/caddy/Caddyfile.prodlike.in: caddyfile-check OK,
#      real `caddy adapt` -> verify OK (the selftest fixture is shaped like this output);
#   2. F-12 / F-13 violations written as real Caddyfiles (12), adapted by real Caddy: verify FAILS;
#      whitelisted top-level directives and non-hitting ones: verify passes (two steps, not
#      "fail on any hit");
#      wac-090: path matchers with Caddy glob semantics ('?', '[w]', a placeholder, '%'), the
#      review wac-088 §2.3 request_header injection, operator-query spelled localhost/[::1]/unix,
#      a dead /m/v1/watcher/extra forwarder, a (?i) extra route: verify FAILS; a glob that
#      cannot hit and a localhost:8183 mobile handle: verify passes;
#   3. the local probe (`probe`) on the fixture as the "production copy": every §9.14.4 item 1
#      Caddy probe, the fallback probes, F-13 (3) cleaned-and-forwarded dot segments and //,
#      raw-forwarded encoded forms and '#', mobile Authorization kept, browser paths behind
#      basic auth.
#   4. wac-090 probe hygiene (review wac-088 🟡-3/🟡-4/🟡-5): the caller's HOME/XDG dirs stay
#      empty and every Caddy call ran with the temporary state dir (G24); the .o0probe copy says
#      admin off, persist_config off, default_bind 127.0.0.1 (G25) and is 0600; the temporary
#      dir is gone afterwards; the printed candidate/snippet sha256 equal the files; a canary
#      "production service" behind the copy's catch-all, an active health check and a second
#      site gets ZERO requests while the probe runs (all dials pinned to stubs); a Caddy
#      wrapper that edits only the RUN config (verify passes) shows the live checks alone catch
#      a fallback 404 with a body (G26) and an Authorization rewrite on a table path; the
#      §2.3 copy fails in verify AND in the live checks; adapt failure leaves no .o0probe and
#      no temporary dir.
#   5. wac-092 (review wac-090 🟡-1..🟡-5): non-ASCII path patterns ('İ', Kelvin sign), '//' path
#      patterns (request_header, rewrite, forwarder) and host matchers inside the site (a
#      middle-label '*', a placeholder, another name) written as real Caddyfiles: verify FAILS; the
#      n07 / n08 / n17b copies fail in verify AND in the live checks; wrapper modes that edit only
#      the RUN config show the live checks alone catch a '//'-only and a Host-only injection (the
#      probe keeps the production host name); a failing probe leaves no .o0probe or temporary dir
#      (R06); SIGTERM during `caddy adapt`, SIGINT while `caddy run` starts, SIGTERM while the
#      cleanup waits for Caddy, SIGHUP then SIGINT: each leaves no .o0probe, no temporary dir, no
#      wrapper or Caddy process; the caller's proxy/OTEL_* variables never reach Caddy (R10).
#   6. wac-094 (review wac-092 🟡-1, 🟡-2): v14..v20 (a concrete parameter value, media/*.png, an unanchored
#      path_regexp, */risks/btcusdt, method PUT + …/accounts/a*, %2[fF], …/channels/[0-9]*) and a dead *.png
#      forwarder written as real Caddyfiles: verify FAILS; *.js/*.css with a response header and an anchored
#      regexp elsewhere: verify passes; v15..v19 as probe copies fail in verify AND in the live checks (the probe
#      sends every parameter line with PROBE_PARAM_VALUES); a wrapper that injects on media/*.png in the RUN config
#      only is caught by the live checks alone; SIGKILL of the probe leaves .o0probe, the temporary dir and a running
#      Caddy, and the next probe run with the same TMPDIR stops that Caddy, removes both and passes.
#   7. wac-097 (review wac-095 🟡-2, 🟡-3): path changes (g15..g18 of r095, handle_path to operator-query without an injection,
#      a reverse_proxy's own rewrite into the prefix, uri strip_suffix x on /m/v1/watcherx*) and regexps whose top-level '|'
#      hid behind []…], [^]…], \Q…\E or [[:alpha:]…] (g01c, g04c, g04d and a.bmp$ forms no probe value reaches) written
#      as real Caddyfiles: verify FAILS; p03 (rewrite /healthz /api/status), p05, p06, uri strip_prefix /api on /api/v2/*
#      and handle /reports/daily/* { uri strip_prefix /reports } to operator-query: verify passes (handle_path /reports/daily/*
#      strips all of /reports/daily and fails); g16, g17, g18 as probe copies fail in verify AND
#      in the live rewrite-entry checks, and so does uri strip_prefix /m/v1 on /m/m/* + an injection (the encoded '..'
#      entry /m/v1/%2e%2e/m/v1/watcher/... found by the wac-097 differential); the fixture's own 10 entries pass.
#      Review wac-096 🔴-1 / 🟡-1 (coordinator's addition to wac-097): reverse_proxy { rewrite /v1/watcher/dialogs } with an
#      injected Authorization, the same with a placeholder, forward_auth { uri /v1/watcher/status }, handle_errors to
#      operator-query, handle_path + invoke of a named route to operator-query, /m/* invoke, handle_response to
#      operator-query: verify FAILS (the first and handle_errors also in the live checks: /foo/x reaches operator-query
#      as /v1/watcher/dialogs with the injected header; a basic-auth 401 enters the error chain and is forwarded);
#      forward_auth /bar/* { uri /v1/auth } and handle_errors { respond } pass.
#      Review wac-096 second round (coordinator): reverse_proxy { rewrite ?a=1 } and { rewrite "#frag" } after handle_path's
#      strip (q1, q4), handle_errors { reverse_proxy <oq> { rewrite ?a=1 } } (q2), and handle_response routes behind a proxy
#      that rewrites its copy to /ping, in the error chain (q3) and after handle_path: verify FAILS; q1 as a probe copy also
#      fails live; an injection made only when the caller sends NO Authorization is caught by the entries' second send.
# Local only: 127.0.0.1 ports, a temporary directory, a random one-off basic-auth password.
set -eo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
O0="$(cd "$HERE/.." && pwd)"
REPO="$(cd "$O0/../../.." && pwd)"
if [ -z "${O0_CADDY_BIN:-}" ]; then echo "CADDY_REAL_TEST_SKIPPED (O0_CADDY_BIN unset)"; exit 0; fi
CADDY="$O0_CADDY_BIN"
"$CADDY" version | grep -q '^v2\.10\.2 ' || { echo "CADDY_REAL_TEST_FAILED need Caddy v2.10.2 (got $("$CADDY" version | cut -d' ' -f1))"; exit 1; }
WORK="$(mktemp -d "${TMPDIR:-/tmp}/o0-caddy-real.XXXXXX")"
CAN_PID=""
# wac-094: besides the canary, stop anything whose command line still names this work dir (a SIGKILLed probe's Caddy
# if a check below failed half-way); only processes of this test run can carry the random $WORK path
trap '[ -z "$CAN_PID" ] || kill "$CAN_PID" 2>/dev/null; for p in $(pgrep -f "$WORK/" 2>/dev/null); do kill -9 "$p" 2>/dev/null; done; rm -rf "$WORK"' EXIT
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
# wac-090 (review wac-088 🟡-1, 🟡-2, G13)
bad("glob-question-rewrite", good.replace(site_import, site_import + "\trewrite /m/v1/w?tcher/status /index.html\n"))
bad("glob-class-rewrite", good.replace(site_import, site_import + "\trewrite /m/v1/[w]atcher/dialogs /index.html\n"))
bad("glob-placeholder-rewrite", good.replace(site_import, site_import + "\trewrite /m/v1/{http.request.uri.query.zz}watcher/status /index.html\n"))
bad("glob-percent-rewrite-conservative", good.replace(site_import, site_import + "\trewrite /m/v1/%77atcher/status /index.html\n"))
bad("glob-request-header-inject", good.replace(site_import, site_import + '\trequest_header /m/v1/w?tcher/trading/accounts Authorization "Bearer {env.SYSTEM_OBSERVER_TOKEN}"\n'))
bad("mixed-case-rewrite", good.replace(site_import, site_import + "\trewrite /M/V1/Watcher/status /index.html\n"))
bad("handle-m-localhost", good.replace(site_import, site_import + mobile_handle.replace("127.0.0.1:8183", "localhost:8183")))
bad("handle-m-ipv6-loopback", good.replace(site_import, site_import + mobile_handle.replace("127.0.0.1:8183", "[::1]:8183")))
bad("handle-m-unix-socket", good.replace(site_import, site_import + mobile_handle.replace("127.0.0.1:8183", "unix//run/oq.sock")))
bad("dead-extra-forwarder", good.replace(site_import, site_import + "\thandle /m/v1/watcher/extra {\n\t\treverse_proxy 127.0.0.1:8183\n\t}\n"))
bad("regexp-extra-route", good.replace(site_import, site_import + "\t@wx path_regexp (?i)^/m/v1/watcher/extra$\n\thandle @wx {\n\t\trespond 204\n\t}\n"))
# wac-092 (review wac-090 🟡-1..🟡-3: n07, n15b, n08, n19, n17b, n18)
obs = 'Authorization "Bearer {env.SYSTEM_OBSERVER_TOKEN}"'
bad("nonascii-dotted-i-inject", good.replace(site_import, site_import + f"\trequest_header /m/v1/watcher/tradİng/accounts {obs}\n"))
bad("nonascii-kelvin-rewrite", good.replace(site_import, site_import + "\trewrite /m/v1/watcher/trading/risKs /index.html\n"))
bad("double-slash-inject", good.replace(site_import, site_import + f"\trequest_header /m//v1/watcher/trading/accounts {obs}\n"))
bad("double-slash-rewrite", good.replace(site_import, site_import + "\trewrite /m/v1//watcher/status /index.html\n"))
bad("double-slash-forwarder", good.replace(site_import, site_import + "\thandle /m//v1/watcher/* {\n\t\treverse_proxy 127.0.0.1:8183\n\t}\n"))
bad("host-middle-wildcard-inject", good.replace(site_import, site_import + f"\t@h host jp-bot.*.wang\n\trequest_header @h {obs}\n"))
bad("host-placeholder-inject", good.replace(site_import, site_import + f"\t@h host {{http.request.host}}\n\trequest_header @h {obs}\n"))
bad("host-alias-redir-conservative", good.replace(site_import, site_import + "\t@alias host alias.balen.wang\n\tredir @alias https://jp-bot.balen.wang{uri}\n"))
# wac-094 (review wac-092 🟡-1, v14..v20): a concrete parameter value, suffix / middle globs, unanchored regexps
bad("v15-param-value-inject", good.replace(site_import, site_import + f"\t@x path /m/v1/watcher/trading/accounts/account-a\n\trequest_header @x {obs}\n"))
bad("v16-media-png-inject", good.replace(site_import, site_import + f"\t@x path /m/v1/watcher/media/*.png\n\trequest_header @x {obs}\n"))
bad("v17-regexp-unanchored-inject", good.replace(site_import, site_import + f"\t@x path_regexp account-a$\n\trequest_header @x {obs}\n"))
bad("v18-middle-glob-inject", good.replace(site_import, site_import + f"\t@x path */risks/btcusdt\n\trequest_header @x {obs}\n"))
bad("v19-method-param-glob-inject", good.replace(site_import, site_import + f"\t@x {{\n\t\tmethod PUT\n\t\tpath /m/v1/watcher/trading/accounts/a*\n\t}}\n\trequest_header @x {obs}\n"))
bad("v14-regexp-pct-inject", good.replace(site_import, site_import + f"\t@x path_regexp %2[fF]\n\trequest_header @x {obs}\n"))
bad("v20-channels-literal-prefix-rewrite", good.replace(site_import, site_import + "\trewrite /m/v1/watcher/trading/channels/[0-9]* /index.html\n"))
bad("png-forwarder-dead", good.replace(site_import, site_import + "\t@png path *.png\n\thandle @png {\n\t\treverse_proxy 127.0.0.1:8183\n\t}\n"))
fine("static-suffix-response-header", good.replace(site_import, site_import + "\t@static path *.js *.css\n\theader @static Cache-Control max-age=3600\n"))
fine("regexp-anchored-elsewhere-rewrite", good.replace(site_import, site_import + "\t@old path_regexp ^/api/(v1|v2)/old$\n\trewrite @old /api/new\n"))
fine("glob-cannot-hit", good.replace(site_import, site_import + "\trewrite /static/?ld /static/old\n"))
fine("mobile-handle-localhost", good.replace("\t\turi strip_prefix /m\n\t\treverse_proxy 127.0.0.1:8183\n\t}\n\thandle /v1/*",
                                             "\t\turi strip_prefix /m\n\t\treverse_proxy localhost:8183\n\t}\n\thandle /v1/*"))
fine("whitelisted-toplevel", good.replace(site_import, site_import + "\tvars o0probe 1\n\theader -Server\n\tmap {path} {o0m} {\n\t\tdefault x\n\t}\n"))
fine("redir-non-hitting", good.replace(site_import, site_import + "\tredir /old/* /new/\n\trewrite /static/x /static/y\n"))
# wac-097 (review wac-095 🟡-2): path changes that bring a path from outside the prefix into it (g15..g18 of r095)
bad("g15-rewrite-static-into-prefix", good.replace(site_import, site_import + f"\t@s path /static/*\n\trequest_header @s {obs}\n\trewrite @s /m/v1/watcher/status\n"))
bad("g16-uri-strip-prefix-into-prefix", good.replace(site_import, site_import + f"\t@s path /x/*\n\trequest_header @s {obs}\n\turi @s strip_prefix /x\n"))
bad("g17-uri-replace-into-prefix", good.replace(site_import, site_import + f"\t@s path /s/*\n\trequest_header @s {obs}\n\turi @s replace /s/ /m/v1/watcher/ 1\n"))
bad("g18-handle-path-to-oq-injected", good.replace(site_import, site_import + f"\thandle_path /x/* {{\n\t\treverse_proxy 127.0.0.1:8183 {{\n\t\t\theader_up {obs}\n\t\t}}\n\t}}\n"))
bad("handle-path-to-oq-plain", good.replace(site_import, site_import + "\thandle_path /x/* {\n\t\treverse_proxy 127.0.0.1:8183\n\t}\n"))
bad("proxy-rewrite-into-prefix", good.replace(site_import, site_import + "\thandle /q/* {\n\t\treverse_proxy 127.0.0.1:8183 {\n\t\t\trewrite /m/v1/watcher/status\n\t\t}\n\t}\n"))
bad("strip-suffix-watcherx", good.replace(site_import, site_import + "\turi /m/v1/watcherx* strip_suffix x\n"))
# wac-097 real-Caddy differential: /m/v1/%2e%2e/m/v1/watcher/status is matched as /m/m/v1/watcher/status; the strip of
# /m/v1 happens on the escaped path and the encoded '..' then pops only /v1: the snippet sees /m/v1/watcher/status
bad("strip-prefix-multiseg-encoded-dots", good.replace(site_import, site_import + f"\t@s path /m/m/*\n\trequest_header @s {obs}\n\turi @s strip_prefix /m/v1\n"))
# wac-097 (review wac-095 🟡-3): classes whose first member is ']', \Q...\E and POSIX classes before a top-level '|'
bad("g01c-re-class-bracket-alt-acct", good.replace(site_import, site_import + f"\t@x path_regexp `^/static[](]|account-a$`\n\trequest_header @x {obs}\n"))
bad("g04c-re-class-bracket-alt-png", good.replace(site_import, site_import + f"\t@x path_regexp `^/static[](]|\\.png$`\n\trequest_header @x {obs}\n"))
bad("g04d-re-negated-class-bracket-alt-acct", good.replace(site_import, site_import + f"\t@x path_regexp `^/static[^](]|account-a$`\n\trequest_header @x {obs}\n"))
bad("re-class-bracket-alt-bmp", good.replace(site_import, site_import + f"\t@x path_regexp `^/static[](]|a\\.bmp$`\n\trequest_header @x {obs}\n"))
bad("re-qe-alt-bmp", good.replace(site_import, site_import + f"\t@x path_regexp `^/static\\Q(\\E|a\\.bmp$`\n\trequest_header @x {obs}\n"))
bad("re-posix-alt-bmp", good.replace(site_import, site_import + f"\t@x path_regexp `^/static[[:alpha:](]|a\\.bmp$`\n\trequest_header @x {obs}\n"))
fine("p03-healthz-rewrite", good.replace(site_import, site_import + "\trewrite /healthz /api/status\n"))
fine("p05-regexp-static-assets", good.replace(site_import, site_import + "\t@assets path_regexp ^/static/.*\\.(js|css)$\n\trewrite @assets /static/bundle.js\n"))
fine("p06-uri-strip-suffix-api", good.replace(site_import, site_import + "\turi /api/* strip_suffix /\n"))
fine("uri-strip-prefix-api-v2", good.replace(site_import, site_import + "\turi /api/v2/* strip_prefix /api\n"))
# handle_path /reports/daily/* would strip ALL of /reports/daily (Caddy strips the matched prefix): /reports/daily/m/v1/watcher/x
# becomes a prefix path, so every handle_path to operator-query fails; a narrower explicit strip is proven outside
fine("handle-uri-strip-reports-to-oq", good.replace(site_import, site_import + "\thandle /reports/daily/* {\n\t\turi strip_prefix /reports\n\t\treverse_proxy 127.0.0.1:8183\n\t}\n"))
bad("handle-path-reports-daily-to-oq", good.replace(site_import, site_import + "\thandle_path /reports/daily/* {\n\t\treverse_proxy 127.0.0.1:8183\n\t}\n"))
# wac-097 (review wac-096 🔴-1): a reverse_proxy's OWN rewrite (reverse_proxy { rewrite }, forward_auth's uri) is a path change
bad("proxy-rewrite-v1-watcher-injected", good.replace(site_import, site_import + f"\thandle /foo/* {{\n\t\treverse_proxy 127.0.0.1:8183 {{\n\t\t\trewrite /v1/watcher/dialogs\n\t\t\theader_up {obs}\n\t\t}}\n\t}}\n"))
bad("proxy-rewrite-v1-watcher-placeholder", good.replace(site_import, site_import + "\thandle /foo/* {\n\t\treverse_proxy 127.0.0.1:8183 {\n\t\t\trewrite /v1/watcher{path}\n\t\t}\n\t}\n"))
bad("forward-auth-uri-v1-watcher", good.replace(site_import, site_import + "\tforward_auth /bar/* 127.0.0.1:8183 {\n\t\turi /v1/watcher/status\n\t}\n"))
fine("forward-auth-uri-v1-auth-on-bar", good.replace(site_import, site_import + "\tforward_auth /bar/* 127.0.0.1:8183 {\n\t\turi /v1/auth\n\t}\n"))
# wac-097 (review wac-096 🟡-1): the error chain, named routes reached by invoke, handle_response routes
bad("handle-errors-to-oq-injected", good.replace(site_import, site_import + f"\thandle_errors {{\n\t\treverse_proxy 127.0.0.1:8183 {{\n\t\t\theader_up {obs}\n\t\t}}\n\t}}\n"))
named_obs = f"&(obs) {{\n\treverse_proxy 127.0.0.1:8183 {{\n\t\theader_up {obs}\n\t}}\n}}\n"
bad("invoke-named-oq-after-handle-path", good.replace(site_import, site_import + "\thandle_path /x/* {\n\t\tinvoke obs\n\t}\n") + named_obs)
bad("handle-m-invoke-named-oq", good.replace(site_import, site_import + "\t@mm path /m/*\n\thandle @mm {\n\t\tinvoke obs\n\t}\n") + named_obs)
bad("dead-extra-invoke-named-oq", good.replace(site_import, site_import + "\thandle /m/v1/watcher/extra {\n\t\tinvoke obs\n\t}\n") + named_obs)
bad("handle-response-to-oq", good.replace(site_import, site_import + "\t@mm path /m/*\n\thandle @mm {\n\t\treverse_proxy 127.0.0.1:7000 {\n\t\t\t@err status 5xx\n\t\t\thandle_response @err {\n\t\t\t\treverse_proxy 127.0.0.1:8183\n\t\t\t}\n\t\t}\n\t}\n"))
fine("handle-errors-respond", good.replace(site_import, site_import + "\thandle_errors {\n\t\trespond \"{err.status_code}\" 502\n\t}\n"))
# review wac-096 second round (coordinator's second addition): an empty uri path part ('?a=1', '#frag') changes nothing and
# proves nothing; handle_response routes get the ORIGINAL request, not the proxy's rewritten copy (q1..q4 of the review)
bad("q1-handle-path-proxy-rewrite-query", good.replace(site_import, site_import + f"\thandle_path /x/* {{\n\t\treverse_proxy 127.0.0.1:8183 {{\n\t\t\trewrite ?a=1\n\t\t\theader_up {obs}\n\t\t}}\n\t}}\n"))
bad("q2-handle-errors-proxy-rewrite-query", good.replace(site_import, site_import + f"\thandle_errors {{\n\t\treverse_proxy 127.0.0.1:8183 {{\n\t\t\trewrite ?a=1\n\t\t\theader_up {obs}\n\t\t}}\n\t}}\n"))
bad("q3-handle-errors-handle-response-original-request", good.replace(site_import, site_import + f"\thandle_errors {{\n\t\treverse_proxy 127.0.0.1:18998 {{\n\t\t\trewrite /ping\n\t\t\t@any status 2xx 3xx 4xx 5xx\n\t\t\thandle_response @any {{\n\t\t\t\treverse_proxy 127.0.0.1:8183 {{\n\t\t\t\t\theader_up {obs}\n\t\t\t\t}}\n\t\t\t}}\n\t\t}}\n\t}}\n"))
bad("q4-handle-path-proxy-rewrite-fragment", good.replace(site_import, site_import + f"\thandle_path /x/* {{\n\t\treverse_proxy 127.0.0.1:8183 {{\n\t\t\trewrite \"#frag\"\n\t\t\theader_up {obs}\n\t\t}}\n\t}}\n"))
bad("handle-path-handle-response-original-request", good.replace(site_import, site_import + "\thandle_path /x/* {\n\t\treverse_proxy 127.0.0.1:7001 {\n\t\t\trewrite /ping\n\t\t\t@any status 2xx 3xx 4xx 5xx\n\t\t\thandle_response @any {\n\t\t\t\treverse_proxy 127.0.0.1:8183\n\t\t\t}\n\t\t}\n\t}\n"))
# a "default token" shape: injected only when the caller sends NO Authorization (the probe sends each entry both ways)
bad("g16-inject-only-without-auth", good.replace(site_import, site_import + f"\t@s {{\n\t\tpath /x/*\n\t\tnot header Authorization *\n\t}}\n\trequest_header @s {obs}\n\turi @s strip_prefix /x\n"))
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
grep -q '^+http://.* {$' "$WORK/probe.txt" && grep -q '^PROBE_LOCAL_ONLY listen=127.0.0.1:[0-9]* host=jp-bot.balen.wang ' "$WORK/probe.txt" \
  && ! grep -q '\$2a\$' "$WORK/probe.txt" \
  && ok "probe records the line diff (site address rewritten), keeps the production host name, prints no bcrypt hash" || bad "probe diff record"

# 4. probe hygiene and local-only pinning (wac-090)
WRAP="$WORK/caddy-wrap"
cat > "$WRAP" <<'WRAPEOF'
#!/usr/bin/env bash
# test wrapper around the real Caddy: logs the state-dir environment of every call; O0_WRAP_MODE edits
# the pinned RUN config only (the probe's verify already passed on the unedited adapt output)
printf '%s HOME=%s XDG_CONFIG_HOME=%s XDG_DATA_HOME=%s\n' "$1" "$HOME" "${XDG_CONFIG_HOME:-}" "${XDG_DATA_HOME:-}" >> "$O0_WRAP_LOG"
# wac-092 R10: which proxy / OTEL_* variables reached this Caddy call (names only; values are test fakes anyway)
[ -z "${O0_WRAP_ENVLOG:-}" ] || printf '%s proxy=[%s] otel=[%s]\n' "$1" "$(env | grep -iE '^(http|https|all|no|ftp)_proxy=' | cut -d= -f1 | sort | tr '\n' ',')" \
  "$(env | grep -E '^OTEL_' | sort | tr '\n' ',')" >> "$O0_WRAP_ENVLOG"
# wac-092 (review wac-090 🟡-4) signal timing: adapt-slow = `caddy adapt` hangs; run-wait = `caddy run` starts 1.5 s late;
# slow-stop = on SIGTERM the wrapper takes 30 s to stop (the probe's cleanup is waiting when the test signals it)
case "${O0_WRAP_SIG:-}:$1" in *adapt-slow*:adapt) echo "adapt $$" >> "$O0_WRAP_MARK"; sleep 30 ;; esac
if [ "$1" = run ] && [ -n "${O0_WRAP_SIG:-}" ]; then
  echo "run $$" >> "$O0_WRAP_MARK"
  case "$O0_WRAP_SIG" in *slow-stop*) trap 'echo stopping >> "$O0_WRAP_MARK"; sleep 30; exit 0' TERM ;; esac
  case "$O0_WRAP_SIG" in *run-wait*) sleep 1.5 ;; esac
  case "$O0_WRAP_SIG" in *slow-stop*) "$O0_CADDY_BIN" "$@" & c=$!; echo "caddy $c" >> "$O0_WRAP_MARK"; wait "$c"; exit $? ;; esac
fi
if [ "$1" = run ] && [ -n "${O0_WRAP_MODE:-}" ]; then
  python3 - "$3" "$O0_WRAP_MODE" <<'PY'
import json, sys
path, mode = sys.argv[1], sys.argv[2]
cfg = json.load(open(path))
def walk(n):
    if isinstance(n, dict):
        edit(n)
        for v in list(n.values()):
            walk(v)
    elif isinstance(n, list):
        for v in n:
            walk(v)
def edit(n):
    if mode == "fallback-body" and n.get("handler") == "static_response" and str(n.get("status_code")) == "404":
        n["body"] = "x"
    if mode == "inject-auth" and any("trading/accounts$" in (m.get("path_regexp") or {}).get("pattern", "") for m in n.get("match") or []):
        n["handle"].insert(0, {"handler": "headers", "request": {"set": {"Authorization": ["Bearer injected"]}}})
    if mode == "strip-auth" and any("trading/accounts$" in (m.get("path_regexp") or {}).get("pattern", "") for m in n.get("match") or []):
        n["handle"].insert(0, {"handler": "headers", "request": {"delete": ["Authorization"]}})
    if mode == "watcherx-to-oq" and isinstance(n.get("routes"), list) and any(
            "^/m/v1/watcher/status$" == (m.get("path_regexp") or {}).get("pattern") for r in n["routes"] for m in r.get("match") or []):
        line = next(r for r in n["routes"] for m in r.get("match") or [] if (m.get("path_regexp") or {}).get("pattern") == "^/m/v1/watcher/status$")
        n["routes"].insert(0, {"match": [{"path": ["/m/v1/watcherx"]}], "handle": json.loads(json.dumps(line["handle"]))})
    # wac-092: a header rewrite that only a '//' request (review n08) or only the production Host (n17b) triggers
    # (inject-dslash: a doubled slash only the per-line '//' variants send; inject-dslash-noauth: only when the caller
    # sends NO Authorization, so only the unauthenticated '//' request sees it)
    # wac-094: only a request for one suffix (review v16 shape) sees it: the per-line parameter values of the probe
    if mode == "inject-suffix" and isinstance(n.get("routes"), list) and any(
            "^/m/v1/watcher/status$" == (m.get("path_regexp") or {}).get("pattern") for r in n["routes"] for m in r.get("match") or []):
        n["routes"].insert(0, {"match": [{"path": ["/m/v1/watcher/media/*.png"]}],
                               "handle": [{"handler": "headers", "request": {"set": {"Authorization": ["Bearer injected"]}}}]})
    if mode in ("inject-dslash", "inject-dslash-noauth", "inject-host") and isinstance(n.get("routes"), list) and any(
            "^/m/v1/watcher/status$" == (m.get("path_regexp") or {}).get("pattern") for r in n["routes"] for m in r.get("match") or []):
        match = {"inject-dslash": {"path": ["/m/v1//watcher/status"]},
                 "inject-dslash-noauth": {"path": ["/m//v1/watcher/trading/accounts"], "not": [{"header": {"Authorization": ["*"]}}]},
                 "inject-host": {"host": ["jp-bot.*.wang"]}}[mode]
        n["routes"].insert(0, {"match": [match], "handle": [{"handler": "headers", "request": {"set": {"Authorization": ["Bearer injected"]}}}]})
walk(cfg)
json.dump(cfg, open(path, "w"))
PY
fi
exec "$O0_CADDY_BIN" "$@"
WRAPEOF
chmod +x "$WRAP"
export O0_WRAP_LOG="$WORK/wrap.log"
probe() { "${T[@]}" probe --paths "$PATHS" --snippet "$SNIP" "$@"; }
sha() { python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"; }

# 4a. G24/G25/🟡-4/sha: sentinel HOME and XDG dirs, TMPDIR in the work dir, --keep to read the copy
SENT="$WORK/sentinel-home"; PT="$WORK/ptmp"; mkdir -p "$SENT" "$PT"; : > "$O0_WRAP_LOG"
PTN="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$PT")"   # as Python's tempfile spells it (no '//')
rc=0; HOME="$SENT" XDG_CONFIG_HOME="$SENT/cfg" XDG_DATA_HOME="$SENT/data" TMPDIR="$PT" \
  probe --caddy "$WRAP" --caddyfile "$WORK/Caddyfile" --keep > "$WORK/probe-iso.txt" 2>&1 || rc=$?
[ "$rc" = 0 ] && grep -q '^CADDY_PROBE_OK' "$WORK/probe-iso.txt" && ok "probe (sentinel HOME, wrapper): $(grep -o 'stub_hits=[^ ]*' "$WORK/probe-iso.txt")" \
  || { bad "probe with sentinel HOME rc=$rc"; grep -E '^FAIL|CADDY_PROBE' "$WORK/probe-iso.txt" | head -5; }
[ -z "$(find "$SENT" -mindepth 1 -print -quit)" ] && ok "nothing written under the caller's HOME / XDG dirs (G24)" \
  || { bad "the probe's Caddy wrote under the caller's HOME"; find "$SENT" | head -5; }
if [ "$(grep -c . "$O0_WRAP_LOG")" -ge 3 ] && ! grep -v "HOME=$PTN/o0-caddy-probe-xdg-[^ ]* XDG_CONFIG_HOME=$PTN/o0-caddy-probe-xdg-[^ ]*/config XDG_DATA_HOME=$PTN/o0-caddy-probe-xdg-[^ ]*/data\$" "$O0_WRAP_LOG" | grep -q .; then
  ok "every Caddy call ($(cut -d' ' -f1 "$O0_WRAP_LOG" | tr '\n' ' ')) ran with the temporary state dir (G24)"
else bad "a Caddy call ran with another HOME/XDG dir"; sed 's/^/    /' "$O0_WRAP_LOG"; fi
[ -z "$(find "$PT" -mindepth 1 -print -quit)" ] && ok "temporary state dir (pinned JSON, autosave) removed" || { bad "temporary dir left"; find "$PT" | head; }
if grep -qx $'\tadmin off' "$WORK/Caddyfile.o0probe" && grep -qx $'\tpersist_config off' "$WORK/Caddyfile.o0probe" \
   && grep -qx $'\tdefault_bind 127.0.0.1' "$WORK/Caddyfile.o0probe" && grep -qx $'\tauto_https off' "$WORK/Caddyfile.o0probe" \
   && [ "$(python3 -c 'import os,sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777))' "$WORK/Caddyfile.o0probe")" = 0o600 ] \
   && grep -Eq '^http://jp-bot\.balen\.wang:[0-9]+ \{$' "$WORK/Caddyfile.o0probe" \
   && grep -q '^PROBE_LOCAL_ONLY listen=127.0.0.1:[0-9]* host=jp-bot.balen.wang admin=off persist=off servers=1 apps=http dials=stubs_only$' "$WORK/probe-iso.txt"; then
  ok "the .o0probe copy (0600) says admin off, persist_config off, default_bind 127.0.0.1, auto_https off, site http://jp-bot.balen.wang:<port> (wac-092); pinned run is local-only (G25)"
else bad "probe copy options / local-only line"; fi
rm -f "$WORK/Caddyfile.o0probe"
if grep -q "candidate_sha256=$(sha "$WORK/Caddyfile") snippet_sha256=$(sha "$WORK/caddy-watcher-gateway.caddy")\$" "$WORK/probe-iso.txt" \
   && grep -q "^PROBE_INPUT candidate_sha256=$(sha "$WORK/Caddyfile") " "$WORK/probe-iso.txt"; then
  ok "probe prints the copy's and the snippet's sha256 (bound to the C-1 gate)"
else bad "probe sha256 line"; fi

# 4b. canary: a "production service" behind the catch-all, an active health check and a second site (bind 0.0.0.0)
( python3 - "$WORK/canary.log" > "$WORK/canary.port" <<'PY' &
import http.server, sys
log = sys.argv[1]
class H(http.server.BaseHTTPRequestHandler):
    def _r(self):
        with open(log, "a") as f:
            f.write(f"{self.command} {self.path}\n")
        self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()
    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = do_OPTIONS = _r
    def log_message(self, *a):
        pass
s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
print(s.server_address[1], flush=True)
s.serve_forever()
PY
  echo $! > "$WORK/canary.pid" )   # a subshell: the canary is not a job of this shell (no "Terminated" notice)
for _ in $(seq 50); do [ -s "$WORK/canary.pid" ] && break; sleep 0.1; done
CAN_PID="$(cat "$WORK/canary.pid")"
for _ in $(seq 50); do [ -s "$WORK/canary.port" ] && break; sleep 0.1; done
CAN="$(cat "$WORK/canary.port")"; : > "$WORK/canary.log"
CAN2="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
mkdir -p "$WORK/canary"; cp "$SNIP" "$WORK/canary/caddy-watcher-gateway.caddy"
python3 - "$WORK/Caddyfile" "$WORK/canary/Caddyfile" "$CAN" "$CAN2" <<'PY'
import sys
good, out, can, can2 = open(sys.argv[1]).read(), sys.argv[2], sys.argv[3], sys.argv[4]
catch_all = "\thandle {\n\t\troot * /srv/trader-dashboard/dist\n\t\tfile_server\n\t}\n"
mobile = "\t\turi strip_prefix /m\n\t\treverse_proxy 127.0.0.1:8183\n\t}\n\thandle /v1/*"
assert good.count(catch_all) == 1 and good.count(mobile) == 1
text = good.replace(catch_all, f"\thandle /o0hc/* {{\n\t\treverse_proxy 127.0.0.1:{can} {{\n\t\t\thealth_uri /health\n\t\t\thealth_interval 200ms\n\t\t}}\n\t}}\n"
                    f"\thandle {{\n\t\treverse_proxy 127.0.0.1:{can}\n\t}}\n")
text = text.replace(mobile, mobile.replace("127.0.0.1:8183", "localhost:8183"))
text += f"http://127.0.0.1:{can2} {{\n\tbind 0.0.0.0\n\treverse_proxy 127.0.0.1:{can}\n}}\n"
open(out, "w").write(text)
PY
rc=0; probe --caddy "$CADDY" --caddyfile "$WORK/canary/Caddyfile" > "$WORK/probe-canary.txt" 2>&1 || rc=$?
sleep 0.5
sink_hits="$(grep -o 'stub_hits=oq:[0-9]*,watcher:[0-9]*,sink:[0-9]*' "$WORK/probe-canary.txt" | sed 's/.*sink://')"
if [ "$rc" = 0 ] && [ ! -s "$WORK/canary.log" ] && [ "${sink_hits:-0}" -gt 0 ] \
   && grep -q "^PROBE_PIN dial 127.0.0.1:$CAN -> sink$" "$WORK/probe-canary.txt" && grep -q '^PROBE_PIN dial localhost:8183 -> oq$' "$WORK/probe-canary.txt" \
   && grep -q '^PROBE_PIN servers dropped (not probed): 1$' "$WORK/probe-canary.txt" && grep -q '^PROBE_PIN reverse_proxy health_checks removed' "$WORK/probe-canary.txt"; then
  ok "canary behind the catch-all, a health check and a second site got 0 requests; the probe's catch-all traffic hit the sink ($sink_hits); localhost:8183 pinned to the operator-query stub"
else bad "canary: rc=$rc canary_requests=$(grep -c . "$WORK/canary.log" || true) sink=${sink_hits:-?}"; grep -E '^FAIL|^PROBE_PIN|CADDY_PROBE' "$WORK/probe-canary.txt" | head -12; fi

# 4c. live checks alone (the wrapper edits only the RUN config; verify passed): G26 fallback body, Authorization rewrite
PT3="$WORK/ptmp3"; mkdir -p "$PT3"
for mode in fallback-body inject-auth strip-auth watcherx-to-oq inject-dslash inject-dslash-noauth inject-host inject-suffix; do
  rc=0; TMPDIR="$PT3" O0_WRAP_MODE="$mode" probe --caddy "$WRAP" --caddyfile "$WORK/Caddyfile" > "$WORK/probe-$mode.txt" 2>&1 || rc=$?
  case "$mode" in
    fallback-body) want='^FAIL fallback .* -> 404 body=1B' ;;
    inject-auth|strip-auth) want="^FAIL forward GET '/m/v1/watcher/trading/accounts'.*Authorization changed" ;;
    watcherx-to-oq) want="^FAIL GET '/m/v1/watcherx' -> 200 reached operator-query or the watcher" ;;
    inject-dslash) want="^FAIL forward GET '/m/v1//watcher/status'.*Authorization changed" ;;   # wac-092 🟡-2
    inject-dslash-noauth) want="^FAIL forward GET '/m//v1/watcher/trading/accounts': the caller's Authorization changed" ;;
    inject-host) want="^FAIL forward GET '/m/v1/watcher/dialogs'.*Authorization changed" ;;                 # wac-092 🟡-3 (Host kept; first line, the print cap is 40)
    inject-suffix) want="^FAIL forward GET '/m/v1/watcher/media/x.png'.*Authorization changed" ;;           # wac-094 🟡-1 (parameter values)
  esac
  # R06 (review wac-090 🟡-5): a probe whose checks FAIL also removes the .o0probe copy and its temporary dir
  if [ "$rc" != 0 ] && grep -q '^CADDY_PROBE_FAILED' "$WORK/probe-$mode.txt" && grep -Eq "$want" "$WORK/probe-$mode.txt" && ! grep -q '^FAIL verify:' "$WORK/probe-$mode.txt" \
     && [ ! -e "$WORK/Caddyfile.o0probe" ] && [ -z "$(find "$PT3" -mindepth 1 -print -quit)" ]; then
    ok "live check alone catches $mode (verify passed, the running Caddy differs); .o0probe and temporary dir removed after the failure"
  else bad "live check missed $mode rc=$rc (o0probe $([ -e "$WORK/Caddyfile.o0probe" ] && echo LEFT || echo gone))"; grep -E '^FAIL|CADDY_PROBE' "$WORK/probe-$mode.txt" | head -5; fi
done

# 4d. review wac-088 §2.3 as a probe copy: verify AND the live checks both refuse it
rc=0; probe --caddy "$CADDY" --caddyfile "$WORK/bad-glob-request-header-inject.Caddyfile" > "$WORK/probe-inject.txt" 2>&1 || rc=$?
if [ "$rc" != 0 ] && grep -q '^FAIL verify:' "$WORK/probe-inject.txt" && grep -q "^FAIL forward GET '/m/v1/watcher/trading/accounts'.*Authorization changed" "$WORK/probe-inject.txt" \
   && [ ! -e "$WORK/bad-glob-request-header-inject.Caddyfile.o0probe" ]; then
  ok "§2.3 request_header /m/v1/w?tcher/trading/accounts: verify fails and the live forward sees the rewritten Authorization (.o0probe removed)"
else bad "§2.3 copy rc=$rc"; grep -E '^FAIL|CADDY_PROBE' "$WORK/probe-inject.txt" | head -5; fi
# 4d'. wac-092 (review wac-090 n07, n08, n17b): as probe copies, verify AND the live checks each refuse them
for spec in "nonascii-dotted-i-inject|forward GET '/m/v1/watcher/trading/accounts'" \
            "double-slash-inject|forward GET '/m//v1/watcher/trading/accounts'" \
            "host-middle-wildcard-inject|forward GET '/m/v1/watcher/dialogs'" \
            "v15-param-value-inject|forward PUT '/m/v1/watcher/trading/accounts/account-a'" \
            "v16-media-png-inject|forward GET '/m/v1/watcher/media/x.png'" \
            "v17-regexp-unanchored-inject|forward DELETE '/m/v1/watcher/trading/accounts/account-a'" \
            "v18-middle-glob-inject|forward DELETE '/m/v1/watcher/trading/risks/BTCUSDT'" \
            "v19-method-param-glob-inject|forward PUT '/m/v1/watcher/trading/accounts/account-a'"; do
  n="${spec%%|*}"; want="${spec#*|}"
  rc=0; probe --caddy "$CADDY" --caddyfile "$WORK/bad-$n.Caddyfile" > "$WORK/probe-$n.txt" 2>&1 || rc=$?
  if [ "$rc" != 0 ] && grep -q '^FAIL verify:' "$WORK/probe-$n.txt" && grep -q "^FAIL $want.*Authorization changed" "$WORK/probe-$n.txt" \
     && [ ! -e "$WORK/bad-$n.Caddyfile.o0probe" ]; then
    ok "probe copy $n: verify fails and the live checks see the injected Authorization ($want)"
  else bad "probe copy $n rc=$rc"; grep -E '^FAIL|CADDY_PROBE' "$WORK/probe-$n.txt" | grep -v '^FAIL verify:' | head -5; fi
done

# 4d''. wac-097 (review wac-095 🟡-2): g16, g17, g18 as probe copies: verify fails AND the live rewrite-entry checks see the
# injected Authorization (the probe sends the entries its path changes turn into prefix-space paths)
for spec in "g16-uri-strip-prefix-into-prefix|rewrite entry GET '/x/m/v1/watcher/" \
            "g17-uri-replace-into-prefix|rewrite entry GET '/s/" \
            "g18-handle-path-to-oq-injected|rewrite entry GET '/x/m/v1/watcher/" \
            "strip-prefix-multiseg-encoded-dots|rewrite entry GET '/m/v1/%2e%2e/m/v1/watcher/" \
            "proxy-rewrite-v1-watcher-injected|rewrite entry GET '/foo/x'" \
            "handle-errors-to-oq-injected|browser /watcher/ without credentials -> 200, stub hits 1" \
            "q1-handle-path-proxy-rewrite-query|rewrite entry GET '/x/m/v1/watcher/" \
            "g16-inject-only-without-auth|rewrite entry GET '/x/m/v1/watcher/status' (without Authorization)"; do
  n="${spec%%|*}"; want="${spec#*|}"
  rc=0; probe --caddy "$CADDY" --caddyfile "$WORK/bad-$n.Caddyfile" > "$WORK/probe-$n.txt" 2>&1 || rc=$?
  if [ "$rc" != 0 ] && grep -q '^FAIL verify:' "$WORK/probe-$n.txt" && grep -q "^FAIL $want" "$WORK/probe-$n.txt" \
     && grep -q '^PROBE_REWRITE_ENTRIES targets=[1-9]' "$WORK/probe-$n.txt" && [ ! -e "$WORK/bad-$n.Caddyfile.o0probe" ]; then
    ok "probe copy $n: verify fails and a live check sees it ($(grep -m1 "^FAIL $want" "$WORK/probe-$n.txt" | cut -c1-110))"
  else bad "probe copy $n rc=$rc"; grep -E '^FAIL|CADDY_PROBE|PROBE_REWRITE' "$WORK/probe-$n.txt" | grep -v '^FAIL verify:' | head -5; fi
done
# the fixture's own path changes (@mobile strip /m, the browser strip /watcher) give entries that pass
if grep -q '^PROBE_REWRITE_ENTRIES targets=10 ' "$WORK/probe.txt" && ! grep -q '^FAIL rewrite entry' "$WORK/probe.txt"; then
  ok "fixture probe: 10 rewrite entries (/m + 4 tails; /watcher + 6 tails incl. /v1/watcher/...) sent with and without Authorization, none reaches a stub wrongly"
else bad "fixture probe rewrite entries: $(grep -m1 '^PROBE_REWRITE_ENTRIES' "$WORK/probe.txt")"; fi

# 4e. adapt fails: no .o0probe (bcrypt hash) and no temporary dir left behind
PT2="$WORK/ptmp2"; mkdir -p "$PT2"
sed 's/^\tencode gzip$/\tencode gzip\n\tno_such_directive_o0 x/' "$WORK/Caddyfile" > "$WORK/bad-adapt.Caddyfile"
rc=0; TMPDIR="$PT2" probe --caddy "$CADDY" --caddyfile "$WORK/bad-adapt.Caddyfile" > "$WORK/probe-badadapt.txt" 2>&1 || rc=$?
if [ "$rc" != 0 ] && grep -q '^CADDY_PROBE_FAILED adapt rc=' "$WORK/probe-badadapt.txt" && [ ! -e "$WORK/bad-adapt.Caddyfile.o0probe" ] \
   && [ -z "$(find "$PT2" -mindepth 1 -print -quit)" ]; then
  ok "adapt failure: .o0probe and the temporary dir removed (review wac-088 🟡-4)"
else bad "adapt-failure cleanup rc=$rc"; ls -la "$WORK"/*.o0probe "$PT2" 2>/dev/null | head; fi
# 4f. wac-092 (review wac-090 🟡-4): SIGINT/SIGTERM/SIGHUP at different moments with the real Caddy. Every run must
# leave no .o0probe (bcrypt hash), an empty TMPDIR (temporary state dir with the pinned JSON), no wrapper or Caddy
# process, and print no bcrypt hash.
sig_case() {  # sig_case <name> <O0_WRAP_SIG> <want-rc> <want-line-ERE> <mark>:<SIG> [<mark>:<SIG>]
  local name="$1" mode="$2" want_rc="$3" want="$4" pt="$WORK/ptsig-$1" mark="$WORK/mark-$1" pid rc=0 step w s p pids alive="" missed=""
  shift 4
  mkdir -p "$pt"; : > "$mark"
  # a simple command, not the probe() function: $! must be the Python process itself
  TMPDIR="$pt" O0_WRAP_SIG="$mode" O0_WRAP_MARK="$mark" "${T[@]}" probe --paths "$PATHS" --snippet "$SNIP" --caddy "$WRAP" \
    --caddyfile "$WORK/Caddyfile" > "$WORK/probe-sig-$name.txt" 2>&1 &
  pid=$!
  for step in "$@"; do
    w="${step%%:*}"; s="${step##*:}"
    for _ in $(seq 400); do grep -q "^$w" "$mark" && break; kill -0 "$pid" 2>/dev/null || break; sleep 0.05; done
    grep -q "^$w" "$mark" || missed="$missed $w"
    kill -s "$s" "$pid" 2>/dev/null || true
  done
  wait "$pid" || rc=$?
  pids="$(awk '$1=="adapt"||$1=="run"||$1=="caddy"{print $2}' "$mark")"
  for _ in $(seq 40); do alive=""; for p in $pids; do kill -0 "$p" 2>/dev/null && alive="$alive $p"; done; [ -z "$alive" ] && break; sleep 0.05; done
  if [ -z "$missed" ] && [ "$rc" = "$want_rc" ] && grep -Eq "$want" "$WORK/probe-sig-$name.txt" && [ ! -e "$WORK/Caddyfile.o0probe" ] \
     && [ -z "$(find "$pt" -mindepth 1 -print -quit)" ] && [ -z "$alive" ] && [ -n "$pids" ] && ! grep -q '\$2a\$' "$WORK/probe-sig-$name.txt"; then
    ok "signal $name: rc=$rc, .o0probe and the temporary dir removed, no wrapper/Caddy left ($(echo $pids | wc -w | tr -d ' ') pid(s) checked)"
  else
    bad "signal $name: rc=$rc (want $want_rc) missed=[$missed] o0probe=$([ -e "$WORK/Caddyfile.o0probe" ] && echo LEFT || echo gone) tmp=[$(ls -A "$pt" | tr '\n' ' ')] alive=[$alive]"
    tail -3 "$WORK/probe-sig-$name.txt" | sed 's/^/    /'; rm -f "$WORK/Caddyfile.o0probe"
  fi
}
sig_case adapt-sigterm adapt-slow 143 '^CADDY_PROBE_INTERRUPTED signal=SIGTERM:' adapt:TERM
sig_case run-sigint run-wait 130 '^CADDY_PROBE_INTERRUPTED signal=SIGINT:' run:INT
sig_case cleanup-sigterm slow-stop 143 '^CADDY_PROBE_INTERRUPTED signal=SIGTERM during cleanup' stopping:TERM
sig_case sighup-then-sigint "run-wait slow-stop" 130 '^CADDY_PROBE_INTERRUPTED signal=SIGINT during cleanup' run:HUP stopping:INT

# 4f'. wac-094 (review wac-092 🟡-2): SIGKILL of the probe leaves the .o0probe, the temporary dir and a RUNNING Caddy
# (its own session); the next probe run with the same TMPDIR reports and cleans exactly those (owner record), then passes.
PK="$WORK/ptkill"; MK="$WORK/mark-kill"; mkdir -p "$PK"; : > "$MK"
TMPDIR="$PK" O0_WRAP_SIG=kill9 O0_WRAP_MARK="$MK" "${T[@]}" probe --paths "$PATHS" --snippet "$SNIP" --caddy "$WRAP" \
  --caddyfile "$WORK/Caddyfile" > "$WORK/probe-kill1.txt" 2>&1 &
kpid=$!
for _ in $(seq 400); do grep -q '^run ' "$MK" && break; kill -0 "$kpid" 2>/dev/null || break; sleep 0.02; done
kill -9 "$kpid" 2>/dev/null || true; wait "$kpid" 2>/dev/null || true
cpid="$(awk '$1=="run"{print $2}' "$MK" | head -1)"
kdir="$(ls -d "$PK"/o0-caddy-probe-xdg-* 2>/dev/null | head -1)"
if [ -n "$cpid" ] && kill -0 "$cpid" 2>/dev/null && [ -e "$WORK/Caddyfile.o0probe" ] && [ -n "$kdir" ] && [ -f "$kdir/o0-probe-owner.json" ]; then
  ok "SIGKILL of the probe leaves .o0probe, $(basename "$kdir") with its owner record and a running Caddy (pid $cpid) - the documented gap"
else bad "SIGKILL residue setup: caddy=${cpid:-none} alive=$(kill -0 "${cpid:-0}" 2>/dev/null && echo y || echo n) o0probe=$([ -e "$WORK/Caddyfile.o0probe" ] && echo y || echo n) dir=${kdir:-none}"; fi
rc=0; TMPDIR="$PK" probe --caddy "$CADDY" --caddyfile "$WORK/Caddyfile" > "$WORK/probe-kill2.txt" 2>&1 || rc=$?
for _ in $(seq 100); do kill -0 "${cpid:-0}" 2>/dev/null || break; sleep 0.05; done
if [ "$rc" = 0 ] && grep -q '^CADDY_PROBE_OK' "$WORK/probe-kill2.txt" \
   && grep -Eq "^PROBE_RESIDUE_CLEANED dir=$(basename "${kdir:-none}") processes_stopped=1 o0probe=removed dir_removed=True\$" "$WORK/probe-kill2.txt" \
   && ! kill -0 "${cpid:-0}" 2>/dev/null && [ ! -e "$WORK/Caddyfile.o0probe" ] && [ -z "$(find "$PK" -mindepth 1 -print -quit)" ] \
   && [ -z "$(pgrep -f "$PK/" 2>/dev/null)" ]; then
  ok "next probe run: $(grep -m1 '^PROBE_RESIDUE_SCAN' "$WORK/probe-kill2.txt"); the orphan Caddy stopped, .o0probe and the dir removed, then CADDY_PROBE_OK"
else bad "residue cleanup rc=$rc caddy_alive=$(kill -0 "${cpid:-0}" 2>/dev/null && echo y || echo n) o0probe=$([ -e "$WORK/Caddyfile.o0probe" ] && echo LEFT || echo gone) tmp=[$(ls -A "$PK" | tr '\n' ' ')]"
  grep -E '^PROBE_RESIDUE|CADDY_PROBE' "$WORK/probe-kill2.txt" | head -8; kill -9 "${cpid:-0}" 2>/dev/null || true; rm -f "$WORK/Caddyfile.o0probe"; fi

# 4g. R10 (review wac-090 🟡-5): the caller's proxy and OTEL_* variables never reach the probe's Caddy
EL="$WORK/wrap-env.log"; : > "$EL"
rc=0; HTTP_PROXY=http://127.0.0.1:9 https_proxy=http://127.0.0.1:9 ALL_PROXY=socks5://127.0.0.1:9 NO_PROXY=x OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:9 \
  O0_WRAP_ENVLOG="$EL" probe --caddy "$WRAP" --caddyfile "$WORK/Caddyfile" > "$WORK/probe-proxyenv.txt" 2>&1 || rc=$?
if [ "$rc" = 0 ] && [ "$(grep -c . "$EL")" -ge 3 ] && ! grep -v '^[a-z]* proxy=\[\] otel=\[OTEL_SDK_DISABLED=true,\]$' "$EL" | grep -q .; then
  ok "proxy and OTEL_* variables of the caller never reach Caddy ($(cut -d' ' -f1 "$EL" | tr '\n' ' ')); OTEL_SDK_DISABLED=true"
else bad "proxy/OTEL environment reached Caddy rc=$rc"; sed 's/^/    /' "$EL"; fi

if cat "$WORK"/probe-*.txt | grep -q '\$2a\$'; then bad "a probe output contains a bcrypt hash"; else ok "no probe output contains a bcrypt hash ($(ls "$WORK"/probe-*.txt | wc -l | tr -d ' ') runs)"; fi

echo "CADDY_REAL_TEST $( [ "$fails" = 0 ] && echo OK || echo FAILED ) checks=$checks failures=$fails caddy=$("$CADDY" version | cut -d' ' -f1)"
[ "$fails" = 0 ]
