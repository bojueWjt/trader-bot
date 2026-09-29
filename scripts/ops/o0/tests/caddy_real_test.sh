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
#      Review wac-099 (wac-099 rework): A1 handle_path /x/* { reverse_proxy /v1/* <oq> }, A6 handle_path + handle /v1/*, A7
#      uri path_regexp then reverse_proxy /v1/*, A8 placeholder rewrite then reverse_proxy /v1/*, C1 intercept handle_response
#      rewrite + forward, E1 handle_path inside handle_errors, E2 CONNECT ^/v1/watcherx$ + strip_suffix x: verify FAILS
#      (A1 and C1 also live); handle_path /x/* { reverse_proxy /reports/* <oq> } and a top-level intercept that only answers pass.
#   8. WGW-1.0.4 (§9.14.6, RS-11..RS-15; task wac-105) - where it differs, this overrides the items above: the fixture's
#      snippet upstream dials 8186 (watcher-gateway); bad-* (F-12/F-13, RS-11 snippet upstream, RS-1 guard, I-2) must
#      FAIL, bad-i2-* and every *-8186 twin with GATEWAY_PORT_EXPOSED; did-* (the pre-1.0.4 operator-query shapes, incl.
#      review wac-099 r2 D2/D5 and the user's P4 forward_auth / A5 / P1 try_files leftovers) PASS with a HINT line; ok-*
#      PASS (p8 unix socket and p13 {env.OQ} with UPSTREAM_UNRESOLVED); V-4 text rule; the probe copies: /m-space path
#      changes reach the 8186 stub (fail), operator-query shapes pass with PROBE_HINT lines. RS-15 live: a runner puts
#      8186 and 8183 on separate stubs in real Caddy: every I-2 case and twin gets a non-table request into the 8186 stub
#      (a request-chosen network_proxy port is refused by Caddy at load time), no PASS/did original does.
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
#    WGW-1.0.4 (task wac-105): bad-* must FAIL (bad-i2-* and every *-8186 twin with GATEWAY_PORT_EXPOSED); ok-* must PASS
#    (an ok-*.want file names a line the output must contain: UPSTREAM_UNRESOLVED, a HINT); did-* are the pre-1.0.4
#    operator-query (8183) shapes: verify PASSES with a HINT DEFENSE_IN_DEPTH line (defense in depth, §9.14.6 V-1 (d)),
#    and each has a bad-<name>-8186 twin (its own dials written as 8186) that must FAIL. *.trig files list the requests
#    the RS-15 live runner (section 8) sends.
python3 - "$WORK" <<'PY'
import json, sys
from pathlib import Path
work = Path(sys.argv[1])
good = (work / "Caddyfile").read_text()
site_import = "\timport watcher_gateway_routes\n"
mobile_handle = "\thandle /m/* {\n\t\turi strip_prefix /m\n\t\treverse_proxy 127.0.0.1:8183\n\t}\n"
panel_open = "\thandle /v1/* {\n\t\treverse_proxy 127.0.0.1:8183 {\n"
upstream_def = "(watcher_gateway_upstream) {\n\turi strip_prefix /m\n\treverse_proxy 127.0.0.1:8186\n}\n"
assert good.count(panel_open) == 1 and good.count(upstream_def) == 1
obs = 'Authorization "Bearer {env.SYSTEM_OBSERVER_TOKEN}"'
def write(kind, name, text, want="", trig=()):
    assert text != good, name
    (work / f"{kind}-{name}.Caddyfile").write_text(text)
    if want:
        (work / f"{kind}-{name}.want").write_text(want)
    if trig:
        (work / f"{kind}-{name}.trig").write_text("\n".join(trig) + "\n")
def bad(name, text, trig=()):
    write("bad", name, text, trig=trig)
def fine(name, text, want="", trig=()):
    write("ok", name, text, want, trig)
def did(name, insert, extra="", want="HINT DEFENSE_IN_DEPTH", trig=(), twin=(":8183", ":8186")):
    """a pre-1.0.4 operator-query shape: PASS with a hint; its own dials as 8186 = the bad twin (I-2)"""
    write("did", name, good.replace(site_import, site_import + insert) + extra, want, trig)
    t_insert, t_extra = insert.replace(*twin), extra.replace(*twin)
    if (t_insert, t_extra) != (insert, extra):
        write("bad", f"{name}-8186", good.replace(site_import, site_import + t_insert) + t_extra, trig=trig)
def panel(new_open):
    return good.replace(panel_open, new_open)
# ---- F-12 / F-13 / structure: still failures
bad("handle-before-import", good.replace(site_import, mobile_handle + site_import))
bad("toplevel-rewrite-after-import", good.replace(site_import, site_import + "\trewrite /m/v1/watcher/dialogs /m/v1/watcher/status\n"))
bad("toplevel-request-header", good.replace(site_import, site_import + f'\trequest_header {obs}\n'))
bad("toplevel-request-header-other", good.replace(site_import, site_import + '\trequest_header X-Other 1\n'))  # V-1 (a): any request header op
bad("toplevel-basic-auth", good.replace(site_import, site_import + "\tbasic_auth /m/* {\n\t\tu " + good.split("o0fixture ")[1].split("\n")[0] + "\n\t}\n"))
bad("route-wrapped", good.replace(site_import, "\troute {\n\t\timport watcher_gateway_routes\n\t}\n"))
bad("handle-path-wrapped", good.replace(site_import, "\thandle_path /m* {\n\t\timport watcher_gateway_routes\n\t}\n"))
bad("global-order-reverse-proxy", good.replace("\tadmin localhost:2019\n", "\tadmin localhost:2019\n\torder reverse_proxy before handle\n")
    .replace(site_import, site_import + "\treverse_proxy /m/* 127.0.0.1:8183\n"))
bad("toplevel-redir-prefix", good.replace(site_import, site_import + "\tredir /m/v1/watcher/* /\n"))
bad("toplevel-uri-strip-suffix", good.replace(site_import, site_import + "\turi /m/v1/watcher/* strip_suffix /never-there\n"))
bad("toplevel-method-rewrite", good.replace(site_import, site_import + "\tmethod /m/v1/watcher/* POST\n"))
bad("toplevel-forward-auth-copy-headers", good.replace(site_import, site_import + "\tforward_auth 127.0.0.1:7000 {\n\t\turi /check\n\t\tcopy_headers Remote-User\n\t}\n"))
bad("glob-question-rewrite", good.replace(site_import, site_import + "\trewrite /m/v1/w?tcher/status /index.html\n"))
bad("glob-class-rewrite", good.replace(site_import, site_import + "\trewrite /m/v1/[w]atcher/dialogs /index.html\n"))
bad("glob-placeholder-rewrite", good.replace(site_import, site_import + "\trewrite /m/v1/{http.request.uri.query.zz}watcher/status /index.html\n"))
bad("glob-percent-rewrite-conservative", good.replace(site_import, site_import + "\trewrite /m/v1/%77atcher/status /index.html\n"))
bad("glob-request-header-inject", good.replace(site_import, site_import + f'\trequest_header /m/v1/w?tcher/trading/accounts {obs}\n'))
bad("mixed-case-rewrite", good.replace(site_import, site_import + "\trewrite /M/V1/Watcher/status /index.html\n"))
bad("handle-m-unix-socket", good.replace(site_import, site_import + mobile_handle.replace("127.0.0.1:8183", "unix//run/oq.sock")))
bad("regexp-extra-route", good.replace(site_import, site_import + "\t@wx path_regexp (?i)^/m/v1/watcher/extra$\n\thandle @wx {\n\t\trespond 204\n\t}\n"))
bad("nonascii-dotted-i-inject", good.replace(site_import, site_import + f"\trequest_header /m/v1/watcher/tradİng/accounts {obs}\n"))
bad("nonascii-kelvin-rewrite", good.replace(site_import, site_import + "\trewrite /m/v1/watcher/trading/risKs /index.html\n"))
bad("double-slash-inject", good.replace(site_import, site_import + f"\trequest_header /m//v1/watcher/trading/accounts {obs}\n"))
bad("double-slash-rewrite", good.replace(site_import, site_import + "\trewrite /m/v1//watcher/status /index.html\n"))
bad("double-slash-forwarder", good.replace(site_import, site_import + "\thandle /m//v1/watcher/* {\n\t\treverse_proxy 127.0.0.1:8183\n\t}\n"))
bad("host-middle-wildcard-inject", good.replace(site_import, site_import + f"\t@h host jp-bot.*.wang\n\trequest_header @h {obs}\n"))
bad("host-placeholder-inject", good.replace(site_import, site_import + f"\t@h host {{http.request.host}}\n\trequest_header @h {obs}\n"))
bad("host-alias-redir-conservative", good.replace(site_import, site_import + "\t@alias host alias.balen.wang\n\tredir @alias https://jp-bot.balen.wang{uri}\n"))
bad("v15-param-value-inject", good.replace(site_import, site_import + f"\t@x path /m/v1/watcher/trading/accounts/account-a\n\trequest_header @x {obs}\n"))
bad("v16-media-png-inject", good.replace(site_import, site_import + f"\t@x path /m/v1/watcher/media/*.png\n\trequest_header @x {obs}\n"))
bad("v17-regexp-unanchored-inject", good.replace(site_import, site_import + f"\t@x path_regexp account-a$\n\trequest_header @x {obs}\n"))
bad("v18-middle-glob-inject", good.replace(site_import, site_import + f"\t@x path */risks/btcusdt\n\trequest_header @x {obs}\n"))
bad("v19-method-param-glob-inject", good.replace(site_import, site_import + f"\t@x {{\n\t\tmethod PUT\n\t\tpath /m/v1/watcher/trading/accounts/a*\n\t}}\n\trequest_header @x {obs}\n"))
bad("v14-regexp-pct-inject", good.replace(site_import, site_import + f"\t@x path_regexp %2[fF]\n\trequest_header @x {obs}\n"))
bad("v20-channels-literal-prefix-rewrite", good.replace(site_import, site_import + "\trewrite /m/v1/watcher/trading/channels/[0-9]* /index.html\n"))
bad("g15-rewrite-static-into-prefix", good.replace(site_import, site_import + f"\t@s path /static/*\n\trequest_header @s {obs}\n\trewrite @s /m/v1/watcher/status\n"))
bad("g16-uri-strip-prefix-into-prefix", good.replace(site_import, site_import + f"\t@s path /x/*\n\trequest_header @s {obs}\n\turi @s strip_prefix /x\n"))
bad("g17-uri-replace-into-prefix", good.replace(site_import, site_import + f"\t@s path /s/*\n\trequest_header @s {obs}\n\turi @s replace /s/ /m/v1/watcher/ 1\n"))
bad("strip-suffix-watcherx", good.replace(site_import, site_import + "\turi /m/v1/watcherx* strip_suffix x\n"))
bad("strip-prefix-multiseg-encoded-dots", good.replace(site_import, site_import + f"\t@s path /m/m/*\n\trequest_header @s {obs}\n\turi @s strip_prefix /m/v1\n"))
bad("g01c-re-class-bracket-alt-acct", good.replace(site_import, site_import + f"\t@x path_regexp `^/static[](]|account-a$`\n\trequest_header @x {obs}\n"))
bad("g04c-re-class-bracket-alt-png", good.replace(site_import, site_import + f"\t@x path_regexp `^/static[](]|\\.png$`\n\trequest_header @x {obs}\n"))
bad("g04d-re-negated-class-bracket-alt-acct", good.replace(site_import, site_import + f"\t@x path_regexp `^/static[^](]|account-a$`\n\trequest_header @x {obs}\n"))
bad("re-class-bracket-alt-bmp", good.replace(site_import, site_import + f"\t@x path_regexp `^/static[](]|a\\.bmp$`\n\trequest_header @x {obs}\n"))
bad("re-qe-alt-bmp", good.replace(site_import, site_import + f"\t@x path_regexp `^/static\\Q(\\E|a\\.bmp$`\n\trequest_header @x {obs}\n"))
bad("re-posix-alt-bmp", good.replace(site_import, site_import + f"\t@x path_regexp `^/static[[:alpha:](]|a\\.bmp$`\n\trequest_header @x {obs}\n"))
named_obs = f"&(obs) {{\n\treverse_proxy 127.0.0.1:8183 {{\n\t\theader_up {obs}\n\t}}\n}}\n"
bad("handle-m-invoke-named-oq", good.replace(site_import, site_import + "\t@mm path /m/*\n\thandle @mm {\n\t\tinvoke obs\n\t}\n") + named_obs)
bad("handle-response-to-oq", good.replace(site_import, site_import + "\t@mm path /m/*\n\thandle @mm {\n\t\treverse_proxy 127.0.0.1:7000 {\n\t\t\t@err status 5xx\n\t\t\thandle_response @err {\n\t\t\t\treverse_proxy 127.0.0.1:8183\n\t\t\t}\n\t\t}\n\t}\n"))
bad("g16-inject-only-without-auth", good.replace(site_import, site_import + f"\t@s {{\n\t\tpath /x/*\n\t\tnot header Authorization *\n\t}}\n\trequest_header @s {obs}\n\turi @s strip_prefix /x\n"))
# ---- RS-11: the snippet upstream (watcher_gateway_upstream) itself
bad("rs11-upstream-8183", good.replace(upstream_def, upstream_def.replace("127.0.0.1:8186", "127.0.0.1:8183")))
bad("rs11-upstream-transport-http", good.replace(upstream_def, upstream_def.replace("reverse_proxy 127.0.0.1:8186\n",
    "reverse_proxy 127.0.0.1:8186 {\n\t\ttransport http {\n\t\t\tread_timeout 30s\n\t\t}\n\t}\n")))
bad("rs11-upstream-header-up-authorization", good.replace(upstream_def, upstream_def.replace("reverse_proxy 127.0.0.1:8186\n",
    f"reverse_proxy 127.0.0.1:8186 {{\n\t\theader_up {obs}\n\t}}\n")))
# ---- I-2 (RS-15 FAIL list): these shapes dial 8186 (live: section 8 shows the 8186 stub gets non-table requests)
# (the panel shapes are triggered with /v1/accounts: /v1/watcher/* is answered by the direct guard first)
bad("i2-panel-8186", panel(panel_open.replace(":8183", ":8186")), trig=("GET /v1/accounts",))
bad("i2-localhost-8186", good.replace(site_import, site_import + "\thandle /g/* {\n\t\treverse_proxy localhost:8186\n\t}\n"), trig=("GET /g/x",))
bad("i2-ipv6-loopback-8186", good.replace(site_import, site_import + "\thandle /g/* {\n\t\treverse_proxy [::1]:8186\n\t}\n"), trig=("GET /g/x",))
bad("i2-port-range-8180-8189", good.replace(site_import, site_import + "\thandle /g/* {\n\t\treverse_proxy 127.0.0.1:8180-8189 {\n\t\t\tlb_policy round_robin\n\t\t}\n\t}\n"),
    trig=("GET /g/x",) * 12)
bad("i2-network-proxy-8186", panel(panel_open + "\t\t\ttransport http {\n\t\t\t\tnetwork_proxy url http://127.0.0.1:8186\n\t\t\t}\n"), trig=("GET /v1/accounts",))
bad("i2-forward-proxy-url-8186", panel(panel_open + "\t\t\ttransport http {\n\t\t\t\tforward_proxy_url http://127.0.0.1:8186\n\t\t\t}\n"), trig=("GET /v1/accounts",))
bad("i2-dynamic-a-8186", good.replace(site_import, site_import + "\thandle /d/* {\n\t\treverse_proxy {\n\t\t\tdynamic a {\n\t\t\t\tname localhost\n\t\t\t\tport 8186\n\t\t\t}\n\t\t}\n\t}\n"),
    trig=("GET /d/x",))
bad("i2-health-upstream-8186", panel(panel_open + "\t\t\thealth_uri /o0h\n\t\t\thealth_interval 200ms\n\t\t\thealth_upstream 127.0.0.1:8186\n"), trig=("WAIT",))
bad("i2-p11-dial-from-request-header", panel(panel_open.replace("127.0.0.1:8183", "{http.request.header.X-Up}")), trig=("GET /v1/accounts|X-Up: {GW}",))
bad("i2-p12-port-from-request-header", panel(panel_open.replace("127.0.0.1:8183", "127.0.0.1:{http.request.header.X-Port}")),
    trig=("GET /v1/accounts|X-Port: {GWPORT}",))
# (Caddy v2.10.2 refuses to LOAD a network_proxy URL with a placeholder port - provision error - so the live runner
# asserts that refusal; the static FAIL stays, it is the task's rule and costs nothing)
bad("i2-network-proxy-from-request-header", panel(panel_open + "\t\t\ttransport http {\n\t\t\t\tnetwork_proxy url http://127.0.0.1:{http.request.header.Y}\n\t\t\t}\n"),
    trig=("LOAD_REFUSED",))
bad("i2-toplevel-forward-auth-8186", good.replace(site_import, site_import + "\tforward_auth 127.0.0.1:8186 {\n\t\turi /check\n\t}\n"), trig=("GET /anything",))
bad("i2-upstream-snippet-imported-by-another-handle", good.replace(site_import, site_import + "\thandle /evil/* {\n\t\timport watcher_gateway_upstream\n\t}\n"),
    trig=("GET /evil/m/v1/watcher/status",))
# ---- RS-15 PASS list: common 8183 writings and unresolvable upstreams (info lines, never a silent "equal")
fine("p8-unix-socket", good.replace(site_import, site_import + "\thandle /u/* {\n\t\treverse_proxy unix//run/app.sock\n\t}\n"), "UPSTREAM_UNRESOLVED")
fine("p9-env-host-literal-port", good.replace(site_import, site_import + "\thandle /e/* {\n\t\treverse_proxy {env.OQ_HOST}:8183\n\t}\n"), trig=("GET /e/v1/watcher/status",))
bad("p9-env-host-literal-port-8186", good.replace(site_import, site_import + "\thandle /e/* {\n\t\treverse_proxy {env.OQ_HOST}:8186\n\t}\n"), trig=("GET /e/v1/watcher/status",))
fine("p10-dynamic-a-8183", good.replace(site_import, site_import + "\thandle /d/* {\n\t\treverse_proxy {\n\t\t\tdynamic a {\n\t\t\t\tname localhost\n\t\t\t\tport 8183\n\t\t\t}\n\t\t}\n\t}\n"),
     trig=("GET /d/v1/watcher/status",))
fine("p13-env-dial", good.replace(site_import, site_import + "\thandle /e/* {\n\t\treverse_proxy {env.OQ}\n\t}\n"), "UPSTREAM_UNRESOLVED")
fine("toplevel-reverse-proxy-v1-8183", good.replace(site_import, site_import + "\treverse_proxy /v1/* 127.0.0.1:8183\n"), trig=("GET /v1/watcher/status",))
bad("toplevel-reverse-proxy-v1-8186", good.replace(site_import, site_import + "\treverse_proxy /v1/* 127.0.0.1:8186\n"))
fine("P1-spa-catch-all-try-files", good.replace("\t\troot * /srv/trader-dashboard/dist\n\t\tfile_server\n",
     "\t\troot * /srv/trader-dashboard/dist\n\t\ttry_files {path} /index.html\n\t\tfile_server\n"), "HINT UNCOMPARABLE_AFTER_SNIPPET")
fine("other-site-direct-guard", good + "http://other.example {\n\timport watcher_gateway_direct_guard\n\treverse_proxy 127.0.0.1:8183\n}\n", "optional direct guard")
# ---- defense in depth (did-*): the pre-1.0.4 operator-query shapes, each with its 8186 twin
did("P4-toplevel-forward-auth", "\tforward_auth 127.0.0.1:7000 {\n\t\turi /check\n\t}\n", twin=("127.0.0.1:7000", "127.0.0.1:8186"), trig=("GET /anything",))
did("handle-m-after-import", mobile_handle, trig=("GET /m/v1/watcherx",))
did("handle-m-localhost", mobile_handle.replace("127.0.0.1:8183", "localhost:8183"), trig=("GET /m/v1/watcherx",))
did("handle-m-ipv6-loopback", mobile_handle.replace("127.0.0.1:8183", "[::1]:8183"), trig=("GET /m/v1/watcherx",))
did("at-m-path-m-v1", "\t@m path /m/v1/*\n\thandle @m {\n\t\turi strip_prefix /m\n\t\treverse_proxy 127.0.0.1:8183\n\t}\n", trig=("GET /m/v1/watcherx",))
did("dead-extra-forwarder", "\thandle /m/v1/watcher/extra {\n\t\treverse_proxy 127.0.0.1:8183\n\t}\n")
did("png-forwarder-dead", "\t@png path *.png\n\thandle @png {\n\t\treverse_proxy 127.0.0.1:8183\n\t}\n", trig=("GET /static/a.png",))
did("g18-handle-path-to-oq-injected", f"\thandle_path /x/* {{\n\t\treverse_proxy 127.0.0.1:8183 {{\n\t\t\theader_up {obs}\n\t\t}}\n\t}}\n", trig=("GET /x/v1/watcher/status",))
did("handle-path-to-oq-plain", "\thandle_path /x/* {\n\t\treverse_proxy 127.0.0.1:8183\n\t}\n", trig=("GET /x/v1/watcher/status",))
did("handle-path-p", "\thandle_path /p/* {\n\t\treverse_proxy 127.0.0.1:8183\n\t}\n", trig=("GET /p/v1/watcher/status",))
did("proxy-rewrite-into-prefix", "\thandle /q/* {\n\t\treverse_proxy 127.0.0.1:8183 {\n\t\t\trewrite /m/v1/watcher/status\n\t\t}\n\t}\n", trig=("GET /q/x",))
did("handle-path-reports-daily-to-oq", "\thandle_path /reports/daily/* {\n\t\treverse_proxy 127.0.0.1:8183\n\t}\n", trig=("GET /reports/daily/v1/watcher/status",))
did("proxy-rewrite-v1-watcher-injected", f"\thandle /foo/* {{\n\t\treverse_proxy 127.0.0.1:8183 {{\n\t\t\trewrite /v1/watcher/dialogs\n\t\t\theader_up {obs}\n\t\t}}\n\t}}\n", trig=("GET /foo/x",))
did("proxy-rewrite-v1-watcher-placeholder", "\thandle /foo/* {\n\t\treverse_proxy 127.0.0.1:8183 {\n\t\t\trewrite /v1/watcher{path}\n\t\t}\n\t}\n", trig=("GET /foo/status",))
did("forward-auth-uri-v1-watcher", "\tforward_auth /bar/* 127.0.0.1:8183 {\n\t\turi /v1/watcher/status\n\t}\n", trig=("GET /bar/x",))
did("handle-errors-to-oq-injected", f"\thandle_errors {{\n\t\treverse_proxy 127.0.0.1:8183 {{\n\t\t\theader_up {obs}\n\t\t}}\n\t}}\n", trig=("GET /watcher/",))
did("invoke-named-oq-after-handle-path", "\thandle_path /x/* {\n\t\tinvoke obs\n\t}\n", extra=named_obs, trig=("GET /x/v1/watcher/status",))
did("dead-extra-invoke-named-oq", "\thandle /m/v1/watcher/extra {\n\t\tinvoke obs\n\t}\n", extra=named_obs)
did("q1-handle-path-proxy-rewrite-query", f"\thandle_path /x/* {{\n\t\treverse_proxy 127.0.0.1:8183 {{\n\t\t\trewrite ?a=1\n\t\t\theader_up {obs}\n\t\t}}\n\t}}\n", trig=("GET /x/v1/watcher/status",))
did("q2-handle-errors-proxy-rewrite-query", f"\thandle_errors {{\n\t\treverse_proxy 127.0.0.1:8183 {{\n\t\t\trewrite ?a=1\n\t\t\theader_up {obs}\n\t\t}}\n\t}}\n", trig=("GET /watcher/",))
did("q3-handle-errors-handle-response-original-request", f"\thandle_errors {{\n\t\treverse_proxy 127.0.0.1:18998 {{\n\t\t\trewrite /ping\n\t\t\t@any status 2xx 3xx 4xx 5xx\n\t\t\thandle_response @any {{\n\t\t\t\treverse_proxy 127.0.0.1:8183 {{\n\t\t\t\t\theader_up {obs}\n\t\t\t\t}}\n\t\t\t}}\n\t\t}}\n\t}}\n",
    trig=("GET /watcher/",))
did("q4-handle-path-proxy-rewrite-fragment", f"\thandle_path /x/* {{\n\t\treverse_proxy 127.0.0.1:8183 {{\n\t\t\trewrite \"#frag\"\n\t\t\theader_up {obs}\n\t\t}}\n\t}}\n", trig=("GET /x/v1/watcher/status",))
did("handle-path-handle-response-original-request", "\thandle_path /x/* {\n\t\treverse_proxy 127.0.0.1:7001 {\n\t\t\trewrite /ping\n\t\t\t@any status 2xx 3xx 4xx 5xx\n\t\t\thandle_response @any {\n\t\t\t\treverse_proxy 127.0.0.1:8183\n\t\t\t}\n\t\t}\n\t}\n",
    trig=("GET /x/v1/watcher/status",))
did("A1-handle-path-proxy-v1-matcher-obs", f"\thandle_path /x/* {{\n\t\treverse_proxy /v1/* 127.0.0.1:8183 {{\n\t\t\theader_up {obs}\n\t\t}}\n\t}}\n", trig=("GET /x/v1/watcher/status",))
did("A5-handle-path-proxy-m-matcher", "\thandle_path /x/* {\n\t\treverse_proxy /m/* 127.0.0.1:8183\n\t}\n", trig=("GET /x/m/v1/watcher/status",))
did("A6-handle-path-nested-handle-v1-obs", f"\thandle_path /x/* {{\n\t\thandle /v1/* {{\n\t\t\treverse_proxy 127.0.0.1:8183 {{\n\t\t\t\theader_up {obs}\n\t\t\t}}\n\t\t}}\n\t}}\n", trig=("GET /x/v1/watcher/status",))
did("A7-uri-path-regexp-proxy-v1-matcher-obs", f"\thandle /x/* {{\n\t\turi path_regexp ^/x/ /\n\t\treverse_proxy /v1/* 127.0.0.1:8183 {{\n\t\t\theader_up {obs}\n\t\t}}\n\t}}\n", trig=("GET /x/v1/watcher/status",))
did("A8-rewrite-placeholder-proxy-v1-matcher-obs", f"\thandle /x/* {{\n\t\trewrite * /v1{{query.p}}\n\t\treverse_proxy /v1/* 127.0.0.1:8183 {{\n\t\t\theader_up {obs}\n\t\t}}\n\t}}\n", trig=("GET /x/a?p=/watcher/status",))
did("C1-intercept-handle-response-rewrite-oq-obs", f"\thandle /x/* {{\n\t\tintercept {{\n\t\t\thandle_response {{\n\t\t\t\trewrite * /v1/watcher/dialogs\n\t\t\t\treverse_proxy 127.0.0.1:8183 {{\n\t\t\t\t\theader_up {obs}\n\t\t\t\t}}\n\t\t\t}}\n\t\t}}\n\t\trespond 404\n\t}}\n",
    trig=("GET /x/anything",))
did("E1-handle-errors-handle-path-oq-obs", f"\thandle_errors {{\n\t\thandle_path /x/* {{\n\t\t\treverse_proxy 127.0.0.1:8183 {{\n\t\t\t\theader_up {obs}\n\t\t\t}}\n\t\t}}\n\t\trespond 404\n\t}}\n", trig=("GET /x/v1/watcher/status",))
did("E2-connect-exact-regexp-strip-suffix", "\t@s {\n\t\tmethod CONNECT\n\t\tpath_regexp ^/v1/watcherx$\n\t}\n\turi @s strip_suffix x\n")
# review wac-099 r2 D2 / D5 (forward_auth between the path change and a matched 8183 forwarder): the hint text is pinned
did("D2-uri-strip-forward-auth-proxy-v1-obs", f"\thandle /x/* {{\n\t\turi strip_prefix /x\n\t\tforward_auth 127.0.0.1:7000 {{\n\t\t\turi /check\n\t\t}}\n\t\treverse_proxy /v1/* 127.0.0.1:8183 {{\n\t\t\theader_up {obs}\n\t\t}}\n\t}}\n",
    want="may forward a path changed into", trig=("GET /x/v1/watcher/dialogs",))
did("D5-uri-path-regexp-forward-auth-proxy-v1-obs", f"\thandle /x/* {{\n\t\turi path_regexp ^/x/ /\n\t\tforward_auth 127.0.0.1:7000 {{\n\t\t\turi /check\n\t\t}}\n\t\treverse_proxy /v1/* 127.0.0.1:8183 {{\n\t\t\theader_up {obs}\n\t\t}}\n\t}}\n",
    want="may forward a path changed into", trig=("GET /x/v1/watcher/dialogs",))
# ---- benign writings (unchanged expectations)
fine("static-suffix-response-header", good.replace(site_import, site_import + "\t@static path *.js *.css\n\theader @static Cache-Control max-age=3600\n"))
fine("regexp-anchored-elsewhere-rewrite", good.replace(site_import, site_import + "\t@old path_regexp ^/api/(v1|v2)/old$\n\trewrite @old /api/new\n"))
fine("glob-cannot-hit", good.replace(site_import, site_import + "\trewrite /static/?ld /static/old\n"))
fine("mobile-handle-localhost", good.replace("\t\turi strip_prefix /m\n\t\treverse_proxy 127.0.0.1:8183\n\t}\n\thandle /v1/*",
                                             "\t\turi strip_prefix /m\n\t\treverse_proxy localhost:8183\n\t}\n\thandle /v1/*"))
fine("whitelisted-toplevel", good.replace(site_import, site_import + "\tvars o0probe 1\n\theader -Server\n\tmap {path} {o0m} {\n\t\tdefault x\n\t}\n"))
fine("redir-non-hitting", good.replace(site_import, site_import + "\tredir /old/* /new/\n\trewrite /static/x /static/y\n"))
fine("p03-healthz-rewrite", good.replace(site_import, site_import + "\trewrite /healthz /api/status\n"))
fine("p05-regexp-static-assets", good.replace(site_import, site_import + "\t@assets path_regexp ^/static/.*\\.(js|css)$\n\trewrite @assets /static/bundle.js\n"))
fine("p06-uri-strip-suffix-api", good.replace(site_import, site_import + "\turi /api/* strip_suffix /\n"))
fine("uri-strip-prefix-api-v2", good.replace(site_import, site_import + "\turi /api/v2/* strip_prefix /api\n"))
fine("handle-uri-strip-reports-to-oq", good.replace(site_import, site_import + "\thandle /reports/daily/* {\n\t\turi strip_prefix /reports\n\t\treverse_proxy 127.0.0.1:8183\n\t}\n"))
fine("forward-auth-uri-v1-auth-on-bar", good.replace(site_import, site_import + "\tforward_auth /bar/* 127.0.0.1:8183 {\n\t\turi /v1/auth\n\t}\n"))
fine("handle-errors-respond", good.replace(site_import, site_import + "\thandle_errors {\n\t\trespond \"{err.status_code}\" 502\n\t}\n"))
fine("handle-path-proxy-reports-matcher", good.replace(site_import, site_import + "\thandle_path /x/* {\n\t\treverse_proxy /reports/* 127.0.0.1:8183\n\t}\n"))
fine("C4-intercept-toplevel-respond", good.replace(site_import, site_import + "\tintercept /x/* {\n\t\thandle_response {\n\t\t\trespond 204\n\t\t}\n\t}\n"))
PY
# RS-1: the direct guard missing from the snippet (a variant with its own snippet copy next to it)
mkdir -p "$WORK/noguard"
python3 - "$SNIP" "$WORK/noguard/caddy-watcher-gateway.caddy" <<'PY'
import sys
t = open(sys.argv[1]).read()
g = "\t@wgw_direct {\n\t\tpath_regexp ^(?i:/v1/watcher)(?:[/\\n%]|$)\n\t}\n\thandle @wgw_direct {\n\t\trespond 404\n\t}\n"
assert t.count(g) == 2
open(sys.argv[2], "w").write(t.replace(g + "}\n(watcher_gateway_direct_guard)", "}\n(watcher_gateway_direct_guard)", 1))
PY
cp "$WORK/Caddyfile" "$WORK/noguard/Caddyfile"
adapt_verify "$WORK/noguard/Caddyfile"
if [ "$ARC" = 0 ] && [ "$VRC" != 0 ] && grep -q "^FAIL direct guard .* present 0 time(s)" "$WORK/noguard/Caddyfile.verify"; then ok "RS-1 snippet without the direct guard: verify fails ($(grep -m1 '^FAIL direct guard' "$WORK/noguard/Caddyfile.verify" | cut -c1-90))"
else bad "RS-1 snippet without the direct guard: adapt $ARC verify $VRC"; fi
for f in "$WORK"/bad-*.Caddyfile; do
  n="$(basename "$f" .Caddyfile)"; adapt_verify "$f"
  if [ "$ARC" != 0 ]; then bad "$n: real caddy adapt failed rc=$ARC ($(tail -1 "$f.err" | cut -c1-120))"
  elif [ "$VRC" = 0 ]; then bad "$n: verify PASSED a violating config"
  elif { [ "${n#bad-i2-}" != "$n" ] || [ "${n%-8186}" != "$n" ]; } && ! grep -q '^FAIL GATEWAY_PORT_EXPOSED' "$f.verify"; then
    bad "$n: verify failed, but without GATEWAY_PORT_EXPOSED ($(grep -m1 -E '^FAIL' "$f.verify" | cut -c1-120))"
  else ok "$n: verify fails ($(grep -m1 -E '^FAIL' "$f.verify" | cut -c1-150))"; fi
done
for f in "$WORK"/ok-*.Caddyfile "$WORK"/did-*.Caddyfile; do
  n="$(basename "$f" .Caddyfile)"; adapt_verify "$f"; want="$(cat "${f%.Caddyfile}.want" 2>/dev/null || true)"
  if [ "$ARC" = 0 ] && [ "$VRC" = 0 ] && { [ -z "$want" ] || grep -qF -- "$want" "$f.verify"; }; then
    ok "$n: verify passes$( [ -n "$want" ] && echo " and prints '$want' ($(grep -m1 -F -- "$want" "$f.verify" | cut -c1-110))")"
  else bad "$n: adapt $ARC verify $VRC want='$want'"; grep -E '^FAIL' "$f.verify" 2>/dev/null | head -3 || true; fi
done
# JSON-level (no stock Caddyfile directive): a non-http app that dials 8186 (I-2 covers every app)
python3 - "$WORK/Caddyfile.json" "$WORK/l4.json" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))
c["apps"]["layer4"] = {"servers": {"l4": {"listen": [":9443"], "routes": [{"handle": [{"handler": "proxy", "upstreams": [{"dial": ["127.0.0.1:8186"]}]}]}]}}}
json.dump(c, open(sys.argv[2], "w"))
PY
if ! "${T[@]}" verify --adapted "$WORK/l4.json" --paths "$PATHS" --snippet "$SNIP" > "$WORK/l4.verify" 2>&1 && grep -q '^FAIL GATEWAY_PORT_EXPOSED \$\.apps\.layer4' "$WORK/l4.verify"; then
  ok "non-http app (layer4) dialing 127.0.0.1:8186: GATEWAY_PORT_EXPOSED"
else bad "non-http app dial 8186 not caught"; fi
for n in handle-before-import route-wrapped handle-path-wrapped; do
  if "${T[@]}" caddyfile-check --caddyfile "$WORK/bad-$n.Caddyfile" >/dev/null 2>&1; then bad "caddyfile-check accepted $n"; else ok "caddyfile-check rejects $n (F-12 text rule)"; fi
done
# V-4 (RS-14): a standalone 8186 outside (watcher_gateway_upstream) fails the text check on its own
for n in i2-panel-8186 i2-localhost-8186 i2-port-range-8180-8189 i2-network-proxy-8186 i2-health-upstream-8186 g18-handle-path-to-oq-injected-8186; do
  if "${T[@]}" caddyfile-check --caddyfile "$WORK/bad-$n.Caddyfile" > "$WORK/cf-$n.txt" 2>&1; then bad "caddyfile-check (V-4) accepted $n"
  elif grep -q '8186 outside (watcher_gateway_upstream)' "$WORK/cf-$n.txt"; then ok "caddyfile-check rejects $n (V-4: 8186 outside the upstream snippet)"
  else bad "caddyfile-check rejected $n for another reason: $(grep -m1 '^FAIL' "$WORK/cf-$n.txt")"; fi
done
if "${T[@]}" caddyfile-check --caddyfile "$WORK/ok-other-site-direct-guard.Caddyfile" > "$WORK/cf-guard.txt" 2>&1 && grep -q 'optional import watcher_gateway_direct_guard' "$WORK/cf-guard.txt"; then
  ok "caddyfile-check accepts the optional direct guard in another site and records it"
else bad "caddyfile-check on the other-site direct guard"; fi
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
sink_hits="$(grep -o 'stub_hits=gw:[0-9]*,oq:[0-9]*,watcher:[0-9]*,sink:[0-9]*' "$WORK/probe-canary.txt" | sed 's/.*sink://' || true)"
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
    watcherx-to-oq) want="^FAIL prefix probe GET '/m/v1/watcherx' -> 200 reached \[\('gw'" ;;   # WGW-1.0.4: the table route's copy now dials the gateway stub
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

# 4d''. wac-097 (review wac-095 🟡-2), WGW-1.0.4 V-3 (2): path changes INTO the /m/v1/watcher space before the snippet
# (g16, g17, the multi-segment strip, the no-Authorization injection) as probe copies: verify fails AND a live rewrite entry
# reaches the GATEWAY stub (not a table-path probe); the operator-query shapes (did-*) as probe copies: the probe PASSES
# (8186 stub hit only by table paths) and the entries that reach operator-query are PROBE_HINT lines
for spec in "bad-g16-uri-strip-prefix-into-prefix|rewrite entry (with Authorization) GET '/x/m/v1/watcher/" \
            "bad-g17-uri-replace-into-prefix|rewrite entry (with Authorization) GET '/s/" \
            "bad-strip-prefix-multiseg-encoded-dots|rewrite entry (with Authorization) GET '/m/v1/%2e%2e/m/v1/watcher/" \
            "bad-g16-inject-only-without-auth|rewrite entry (without Authorization) GET '/x/m/v1/watcher/status'"; do
  n="${spec%%|*}"; want="${spec#*|}"
  rc=0; probe --caddy "$CADDY" --caddyfile "$WORK/$n.Caddyfile" > "$WORK/probe-$n.txt" 2>&1 || rc=$?
  if [ "$rc" != 0 ] && grep -q '^FAIL verify:' "$WORK/probe-$n.txt" && grep -qF "FAIL $want" "$WORK/probe-$n.txt" \
     && [ ! -e "$WORK/$n.Caddyfile.o0probe" ]; then
    ok "probe copy $n: verify fails and a live check sees it ($(grep -m1 -F "FAIL $want" "$WORK/probe-$n.txt" | cut -c1-120))"
  else bad "probe copy $n rc=$rc"; grep -E '^FAIL|CADDY_PROBE|PROBE_REWRITE' "$WORK/probe-$n.txt" | grep -v '^FAIL verify:' | head -5; fi
done
for spec in "did-g18-handle-path-to-oq-injected|rewrite entry (with Authorization) GET '/x/" \
            "did-proxy-rewrite-v1-watcher-injected|rewrite entry (with Authorization) GET '/foo/x'" \
            "did-handle-errors-to-oq-injected|browser /watcher/ without credentials -> 200" \
            "did-q1-handle-path-proxy-rewrite-query|rewrite entry (with Authorization) GET '/x/" \
            "did-A1-handle-path-proxy-v1-matcher-obs|rewrite entry (with Authorization) GET '/x/v1/watcher/" \
            "did-C1-intercept-handle-response-rewrite-oq-obs|rewrite entry (with Authorization) GET '/x/x'"; do
  n="${spec%%|*}"; want="${spec#*|}"
  rc=0; probe --caddy "$CADDY" --caddyfile "$WORK/$n.Caddyfile" > "$WORK/probe-$n.txt" 2>&1 || rc=$?
  if [ "$rc" = 0 ] && grep -q '^CADDY_PROBE_OK' "$WORK/probe-$n.txt" && grep -qF "PROBE_HINT DEFENSE_IN_DEPTH $want" "$WORK/probe-$n.txt" \
     && [ ! -e "$WORK/$n.Caddyfile.o0probe" ]; then
    ok "probe copy $n: passes (8186 stub hit by table paths only); operator-query shape reported: $(grep -m1 -F "PROBE_HINT DEFENSE_IN_DEPTH $want" "$WORK/probe-$n.txt" | cut -c1-110)"
  else bad "probe copy $n rc=$rc"; grep -E '^FAIL|CADDY_PROBE|PROBE_HINT' "$WORK/probe-$n.txt" | head -5; fi
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

# 8. RS-15 live (WGW-1.0.4 §9.14.6; task wac-105): real Caddy runs each variant with every 8186 dial (any loopback
#    spelling, forward proxy URL, health-check upstream, dynamic a port, a request-chosen port) pointing at a local
#    "gateway" stub and 8183 at a separate operator-query stub; every other upstream goes to a sink. Each I-2 FAIL case
#    and every 8186 twin must get a NON-table request into the gateway stub (the static FAIL is a real exposure); the
#    PASS and defense-in-depth originals must not (their table paths are the probe's business, sections 3 and 4d'').
cat > "$WORK/live_i2.py" <<'LIVEPY'
import json, os, re, signal, socket, subprocess, sys, threading, time, http.server
from pathlib import Path
caddy, o0dir, cfile, expect = sys.argv[1], sys.argv[2], Path(sys.argv[3]), sys.argv[4]
reqs = sys.argv[5:]
sys.path.insert(0, o0dir)
import o0_caddy_watcher_routes as r
hits = []
def stub(label):
    class H(http.server.BaseHTTPRequestHandler):
        def _r(self):
            hits.append((label, self.command, self.path))
            self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()
        do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = do_OPTIONS = do_CONNECT = _r
        def log_message(self, *a):
            pass
    s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return s, f"127.0.0.1:{s.server_address[1]}", str(s.server_address[1])
(gs, gw, gwport), (os_, oq, _), (ws, wa, _), (ss, sink, _) = stub("gw"), stub("oq"), stub("watcher"), stub("sink")
def free():
    x = socket.socket(); x.bind(("127.0.0.1", 0)); n = x.getsockname()[1]; x.close(); return n
port, hp, hsp = free(), free(), free()
live = cfile.with_name(cfile.name + ".live")
live.write_text(r.probe_caddyfile(cfile.read_text(), "jp-bot.balen.wang", port, hp, hsp, "jp-bot.balen.wang"))
env = dict(os.environ, OQ_HOST="127.0.0.1", OQ=oq)
proc = None
try:
    a = subprocess.run([caddy, "adapt", "--adapter", "caddyfile", "--config", str(live)], capture_output=True, text=True, env=env)
    if a.returncode:
        print(f"LIVE_I2_ERROR adapt rc={a.returncode}"); sys.exit(2)
    cfg = json.loads(a.stdout)
    def sub(v):
        v = re.sub(r"(?:127\.0\.0\.1|localhost|\[::1\]|\{env\.OQ_HOST\}):8186(?![0-9])", gw, v)
        v = re.sub(r"(?:127\.0\.0\.1|localhost|\[::1\]|\{env\.OQ_HOST\}):8183(?![0-9])", oq, v)
        return re.sub(r"(?:127\.0\.0\.1|localhost|\[::1\]):9090(?![0-9])", wa, v)
    def walk(n):
        if isinstance(n, dict):
            for k, v in list(n.items()):
                if isinstance(v, str):
                    n[k] = sub(v)
                    if k == "dial" and n[k] not in (gw, oq, wa) and re.fullmatch(r"(127\.0\.0\.1|localhost|\[::1\]):[0-9]+", n[k]):
                        n[k] = sink
                else:
                    walk(v)
            if n.get("source") == "a" and "port" in n:
                n["name"] = "127.0.0.1"
                n["port"] = {"8186": gwport, "8183": oq.rsplit(":", 1)[1]}.get(str(n["port"]), n["port"])
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(cfg)
    run_json = cfile.with_name(cfile.name + ".live.json")
    run_json.write_text(json.dumps(cfg))
    proc = subprocess.Popen([caddy, "run", "--config", str(run_json)], env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(150):
        if proc.poll() is not None:
            break
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close(); break
        except OSError:
            time.sleep(0.1)
    if proc.poll() is not None:
        print(f"LIVE_I2 {cfile.stem} caddy_run_refused=yes rc={proc.returncode}")
        sys.exit(0 if reqs == ["LOAD_REFUSED"] else 1)
    if reqs == ["LOAD_REFUSED"]:
        print(f"LIVE_I2 {cfile.stem} caddy_run_refused=no (expected Caddy to refuse the config)")
        sys.exit(1)
    for spec in reqs:
        if spec == "WAIT":
            time.sleep(1.5); continue
        line, *hdrs = spec.split("|")
        method, target = line.split(" ", 1)
        extra = "".join(h.replace("{GWPORT}", gwport).replace("{GW}", gw) + "\r\n" for h in hdrs)
        c = socket.create_connection(("127.0.0.1", port), timeout=5)
        c.sendall(f"{method} {target} HTTP/1.1\r\nHost: jp-bot.balen.wang\r\n{extra}Content-Length: 0\r\nConnection: close\r\n\r\n".encode())
        while c.recv(65536):
            pass
        c.close()
    time.sleep(0.3)
finally:
    if proc is not None and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL); proc.wait()
        except (ProcessLookupError, PermissionError):
            proc.wait()
    for f in (live, cfile.with_name(cfile.name + ".live.json")):
        f.unlink(missing_ok=True)
gwh = [h for h in hits if h[0] == "gw"]
print(f"LIVE_I2 {cfile.stem} expect={expect} gw_hits={len(gwh)} oq_hits={sum(1 for h in hits if h[0] == 'oq')} "
      f"gw_paths={sorted(set(h[2] for h in gwh))[:3]}")
sys.exit(0 if (bool(gwh) if expect == "gw" else not gwh) else 1)
LIVEPY
live_n=0
for tf in "$WORK"/*.trig; do
  f="${tf%.trig}.Caddyfile"; n="$(basename "$f" .Caddyfile)"; reqs=()   # a did-* twin (bad-*-8186) carries the same requests
  while IFS= read -r l; do [ -n "$l" ] && reqs+=("$l"); done < "$tf"
  case "$n" in bad-*) exp=gw ;; *) exp=no-gw ;; esac
  if (cd "$(dirname "$f")" && HOME="$WORK" python3 "$WORK/live_i2.py" "$CADDY" "$O0" "$f" "$exp" "${reqs[@]}") > "$WORK/live-$n.txt" 2>&1; then
    ok "RS-15 live $n: $(tail -1 "$WORK/live-$n.txt")"; live_n=$((live_n + 1))
  else bad "RS-15 live $n: $(tail -1 "$WORK/live-$n.txt")"; fi
done
[ "$live_n" -ge 40 ] && ok "RS-15 live: $live_n real-Caddy runs (8186 and 8183 on separate stubs)" || bad "RS-15 live ran only $live_n cases"

if cat "$WORK"/probe-*.txt | grep -q '\$2a\$'; then bad "a probe output contains a bcrypt hash"; else ok "no probe output contains a bcrypt hash ($(ls "$WORK"/probe-*.txt | wc -l | tr -d ' ') runs)"; fi

echo "CADDY_REAL_TEST $( [ "$fails" = 0 ] && echo OK || echo FAILED ) checks=$checks failures=$fails caddy=$("$CADDY" version | cut -d' ' -f1)"
[ "$fails" = 0 ]
