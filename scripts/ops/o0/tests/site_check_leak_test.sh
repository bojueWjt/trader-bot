#!/usr/bin/env bash
# Local leak test for o0_site_check.sh: runs the READ-ONLY site check in execute
# mode against stubbed docker/systemctl/caddy/curl/... that emit sentinel secret
# values, then asserts that no sentinel reaches the captured output.
# Runs on macOS or Linux; touches nothing outside a temporary directory.
set -eo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SITE="$HERE/../o0_site_check.sh"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/o0-site-leak.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
STUB="$WORK/bin"
mkdir -p "$STUB" "$WORK/srv-trader-v3" "$WORK/trader/services/telegram-watcher/lib" "$WORK/etc"

S1="SENTINELgatewayTOKEN0123456789abcdefXYZ"
S2="SENTINELriskADMIN0123456789abcdefXYZ"
S3='$2a$14$SENTINELbcryptHASHabcdefghijklmnopqrstuvwxyzABCDEFGH01'
S4="SENTINELliteralHEADER0123456789abcdef"
S5="SENTINELcomposeSECRET0123456789abcdef"
S6="SENTINELunitENV0123456789abcdefXYZ"
# review wac-032 🔴-5 shapes + short literals that no pattern rule can recognise
S7="SENTINELmatcherBEARER0123456789abcdefXYZ"   # header MATCHER value "Bearer <literal>"
S8="pw7replace"                                  # header replace operation
S9="pw7staticbody"                               # static_response.body
S10="pw7short"                                   # short literal header value / respond body / compose env
S11="pw7query"                                   # uri query value
# review wac-032-r2 🔴-1: secrets INSIDE paths (the long-token rule never looked behind '/')
S12="qctbhy0a32hucub"                            # probe-token path (repo Caddyfile.host:15 shape)
S13="9f8e7d6c5b4a39281706f5e4d3c2b1a0ffeeddcc"   # 40-hex webhook path token
S14="bot7654321098:AAHpathBOTsentinel0123456789abcd"   # bot token inside a path_regexp
S15="SENTINELpathsecretXYZ0123456789abcdef"      # token inside a rewrite uri
GOOD_RE='^/m/v1/watcher/trading/risks/[^/]+$'    # a legitimate gateway pattern must stay readable
# review wac-032-r3 🟡-1: tokens cut into short pieces by an escape, '.', '-' or '+' (raw base64);
# 🟡-3: the hex rule on its own (digit-only / letter-only hex) and the exact 12-char threshold
R3_NEEDLES=(ab12cd34 ef56gh78 ij90kl12 SENTINELre gex0123456789 ab12cd34ef 56gh78ij90 Ab3dEfG5hI jK7lMnO9pQ 4829105738291045 DEADBEEFCAFEBABE a1b2c3d4e5f6)

cat > "$WORK/etc/v3.env" <<EOF
WATCHER_BASIC_AUTH_HASH=$S3
CADDY_DOMAIN=jp-bot.balen.wang
SYSTEM_OBSERVER_TOKEN=$S2
EOF
cat > "$WORK/etc/Caddyfile" <<EOF
jp-bot.balen.wang {
	@watcher path /watcher/* /api/*
	handle @watcher {
		basic_auth { balen {\$WATCHER_BASIC_AUTH_HASH} }
		reverse_proxy 127.0.0.1:9090
	}
	handle /v1/* {
		reverse_proxy 127.0.0.1:8183 {
			header_up Authorization "Bearer $S4"
			header_up X-Api-Key $S10
		}
	}
	@hook header Authorization "Bearer $S7"
	respond @hook "$S9" 200
	handle /$S12 {
		respond 200
	}
	handle /hook/$S13/* {
		reverse_proxy 127.0.0.1:7000
	}
	@tg path_regexp ^/$S14/.*\$
	@wgw_risk path_regexp $GOOD_RE
	rewrite /legacy/* /api/$S15
	@r3a path_regexp ^/hook/ab12cd34\-ef56gh78\-ij90kl12\$
	@r3b path_regexp ^/k/SENTINELre\.gex0123456789\$
	handle /k/ab12cd34ef.56gh78ij90 {
		respond 204
	}
	handle /k/Ab3dEfG5hI+x/jK7lMnO9pQ== {
		respond 204
	}
	handle /n/4829105738291045 {
		respond 204
	}
	rewrite /t/* /t/a1b2c3d4e5f6
	root * /srv/DEADBEEFCAFEBABE
}
EOF
cat > "$WORK/oq.env" <<EOF
RISK_ADMIN_TOKEN=$S2
WATCHER_TRADING_DB=/srv/trader-v3/state/operator-query-risk/trading-risk.db
EOF
cat > "$WORK/trader/docker-compose.yml" <<EOF
services:
  watcher:
    environment:
      SOME_API_KEY: $S5
      UPSTREAM_URL: $S10
      WATCHER_HOST: 0.0.0.0
  other:
    image: x
EOF
echo 'module.exports = {};' > "$WORK/trader/services/telegram-watcher/server.js"
python3 - "$WORK/watcher.db" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.executescript("""CREATE TABLE account_configs (account_id TEXT PRIMARY KEY, api_key TEXT, api_secret TEXT, account_type TEXT);
INSERT INTO account_configs VALUES ('a','SENTINELdbKEY0123456789abcdef','SENTINELdbSECRET0123456789abcdef','main');
CREATE TABLE channel_routing (channel_id TEXT, target_account_id TEXT);
CREATE TABLE symbol_risk_configs (symbol TEXT, risk_ratio REAL);""")
c.commit()
PY
cp "$WORK/watcher.db" "$WORK/replica.db"
# S-00 node heartbeat parameters (review wac-032-r2 🟡-7): a node config with a LITERAL token and
# a node env with a token; only the numbers may come out
mkdir -p "$WORK/node" "$WORK/nodeapp/runtime" "$WORK/nodeapp/app"
printf '{"control_plane": {"base_url": "http://cp:8080", "token": "SENTINELnodeCFGtoken0123456789", "heartbeat_timeout_seconds": 15}}\n' > "$WORK/node/account-a.json"
printf 'DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 2.0\n' > "$WORK/nodeapp/runtime/control_plane_session.py"
printf 'session = NodeControlPlaneSession(heartbeat=lambda: None)\n' > "$WORK/nodeapp/app/node.py"

ADAPTED_JSON="{\"apps\":{\"http\":{\"servers\":{\"srv0\":{\"listen\":[\":443\"],\"routes\":[{\"match\":[{\"header\":{\"Authorization\":[\"Bearer $S7\"]},\"path\":[\"/hook\"]}],\"handle\":[{\"handler\":\"headers\",\"request\":{\"replace\":{\"X-Up\":[{\"search\":\"a\",\"replace\":\"$S8\"}]},\"delete\":[\"X-Watcher-Actor\"]}},{\"handler\":\"static_response\",\"status_code\":200,\"body\":\"$S9\"}]},{\"handle\":[{\"handler\":\"vars\",\"k\":\"$S10\"},{\"handler\":\"rewrite\",\"strip_path_prefix\":\"/m\",\"uri\":\"/x?token=$S11\"},{\"handler\":\"authentication\",\"providers\":{\"http_basic\":{\"accounts\":[{\"username\":\"balen\",\"password\":\"$S3\"}]}}},{\"handler\":\"reverse_proxy\",\"headers\":{\"request\":{\"set\":{\"Authorization\":[\"Bearer $S4\"],\"X-Other\":[\"$S10\"],\"X-Watcher-Proxy-Auth\":[\"{env.WATCHER_BROWSER_PROXY_TOKEN}\"]}}},\"upstreams\":[{\"dial\":\"127.0.0.1:9090\"}]}]},\
{\"match\":[{\"path\":[\"/$S12\"]}],\"handle\":[{\"handler\":\"static_response\",\"status_code\":200}]},\
{\"match\":[{\"path\":[\"/hook/$S13/*\"]}],\"handle\":[{\"handler\":\"reverse_proxy\",\"upstreams\":[{\"dial\":\"127.0.0.1:7000\"}]}]},\
{\"match\":[{\"path_regexp\":{\"name\":\"tg\",\"pattern\":\"^/$S14/.*\$\"}}],\"handle\":[{\"handler\":\"reverse_proxy\",\"upstreams\":[{\"dial\":\"127.0.0.1:7001\"}]}]},\
{\"match\":[{\"path_regexp\":{\"name\":\"wgw_risk\",\"pattern\":\"$GOOD_RE\"}}],\"handle\":[{\"handler\":\"reverse_proxy\",\"upstreams\":[{\"dial\":\"127.0.0.1:8183\"}]}]},\
{\"handle\":[{\"handler\":\"rewrite\",\"uri\":\"/api/$S15\"}]}]}}}}}"
# the r3 shapes as matchers, rewrite targets and roots (built in python: JSON escapes of '\')
ADAPTED_JSON="$(python3 - "$ADAPTED_JSON" <<'PY'
import json, sys
cfg = json.loads(sys.argv[1])
shapes = [r"^/hook/ab12cd34\-ef56gh78\-ij90kl12$", r"^/k/SENTINELre\.gex0123456789$", "/k/ab12cd34ef.56gh78ij90",
          "/k/Ab3dEfG5hI+x/jK7lMnO9pQ==", "/n/4829105738291045", "/h/DEADBEEFCAFEBABE", "/t/a1b2c3d4e5f6"]
routes = cfg["apps"]["http"]["servers"]["srv0"]["routes"]
for i, s in enumerate(shapes):
    routes.append({"match": [{"path": [s]}, {"path_regexp": {"name": f"r3_{i}", "pattern": s}}],
                   "handle": [{"handler": "rewrite", "uri": s, "strip_path_prefix": s}, {"handler": "file_server", "root": s}]})
print(json.dumps(cfg, separators=(",", ":")))
PY
)"

stub() { printf '#!/usr/bin/env bash\n%s\n' "$2" > "$STUB/$1"; chmod +x "$STUB/$1"; }
stub id 'echo 0'
stub docker "case \"\$*\" in
  *psql*) echo 'account-a ACTIVE abcdef012345 hb_age=1.1';;
  ps\ --format*) echo 'trader-v3-node-a';;
  *Config.Env*trader-v3-node-a*) printf '%s\n' 'NODE_CONFIG_PATH=$WORK/node/account-a.json' 'CONTROL_PLANE_ACCOUNT_A_TOKEN=SENTINELnodeENVtoken0123456789';;
  exec\ trader-v3-node-*) python3 -c \"\$5\" \"\$6\" '$WORK/nodeapp';;
  *Config.Env*) printf '%s\n' 'WATCHER_GATEWAY_TOKEN=$S1' 'TRADER_TRADING_DB_PATH=/data/watcher-trading.db' 'WATCHER_ALERT_BOT_TOKEN=$S2';;
  *PortBindings*) echo '{\"9100/tcp\":[{\"HostIp\":\"127.0.0.1\",\"HostPort\":\"9090\"}]}';;
  *Labels*) echo '{\"com.docker.compose.project\":\"trader\"}';;
  *Mounts*) echo 'volume /var/lib/docker/volumes/trader_signal-data/_data -> /data rw=true';;
  *inspect*) echo 'image=sha256:abc config_image=trader-watcher started=x restarts=0 health=healthy';;
  logs*) echo '[db] Failed to ensure trading tables: SENTINEL?';;
esac"
stub systemctl "case \"\$*\" in
  *'-p EnvironmentFiles --value'*) echo '$WORK/oq.env (ignore_errors=no)';;
  *'-p WorkingDirectory --value'*) echo '$WORK/srv-trader-v3/services/control-plane/api';;
  cat*) printf '%s\n' 'ExecStart=/srv/trader-v3/.venv-cp/bin/uvicorn read_api:app --workers 2' 'ExecStartPre=/usr/bin/curl -H Authorization: Bearer $S10 http://127.0.0.1:8183/healthz' 'Environment=FEEDER_TOKEN=$S6' 'EnvironmentFile=$WORK/oq.env';;
  *) echo 'ActiveState=active';;
esac"
stub caddy "case \"\$1\" in version) echo 'v2.8.4 h1:stub';; adapt) printf '%s' '$ADAPTED_JSON';; esac"
stub curl "case \"\$*\" in *2019/config*) printf '%s' '$ADAPTED_JSON';; *) echo 401;; esac"
stub ss "echo '127.0.0.1:9090 users:((\"docker-proxy\",pid=1,fd=4))'"
stub stat "echo \"600 root:root 1 2026 \${@: -1}\""
stub journalctl "echo 'Authorization: Bearer $S2'"
stub sha256sum "shasum -a 256 \"\$@\""
stub hostname 'echo jp-24-stub'
mkdir -p "$WORK/srv-trader-v3/services/control-plane/api" "$WORK/srv-trader-v3/services/control-plane/security"
echo 'x = 1' > "$WORK/srv-trader-v3/services/control-plane/api/read_api.py"
echo 'y = 1' > "$WORK/srv-trader-v3/services/control-plane/security/principal.py"

OUT="$WORK/out.txt"
PATH="$STUB:$PATH" JP24_MARKER="$WORK/srv-trader-v3" CADDYFILE="$WORK/etc/Caddyfile" CADDY_ENV="$WORK/etc/v3.env" \
  WATCHER_ROOT="$WORK/trader" WATCHER_DB="$WORK/watcher.db" REPLICA_DB="$WORK/replica.db" VENV_PY=python3 HB_SAMPLE_GAP=0 \
  bash -s -- --execute --auth-id O0-A01 < "$SITE" > "$OUT" 2>&1 || true

[ -z "${O0_KEEP_OUT:-}" ] || cp "$OUT" "$O0_KEEP_OUT"
sections=$(grep -c '^=== S-' "$OUT" || true)
leaks=0
SENTINELS=("$S1" "$S2" "$S3" "$S4" "$S5" "$S6" "$S7" "$S8" "$S9" "$S10" "$S11" "$S12" "$S13" "$S14" "$S15" SENTINELdbKEY SENTINELdbSECRET SENTINELbcrypt pw7
           qctbhy 9f8e7d6c5b4a AAHpathBOT SENTINELpath 7654321098 "${R3_NEEDLES[@]}" SENTINELnodeCFG SENTINELnodeENV)
for s in "${SENTINELS[@]}"; do
  if grep -qF -- "$s" "$OUT"; then echo "LEAK ${s:0:18}..." ; leaks=$((leaks + 1)); fi
done
grep -q 'O0_SITE_CHECK_DONE' "$OUT" || { echo "site check did not finish"; tail -20 "$OUT"; exit 1; }
if [ "$sections" -lt 19 ]; then echo "only $sections sections ran"; exit 1; fi
if [ "$leaks" -gt 0 ]; then echo "LEAK_TEST_FAILED leaks=$leaks"; exit 1; fi
grep -q '<literal len=' "$OUT" || { echo "literal header was not flagged"; exit 1; }
# the skeleton keeps what the checklist needs (structure, placeholders, dials)
for want in '"dial": "127.0.0.1:9090"' '{env.WATCHER_BROWSER_PROXY_TOKEN}' '"strip_path_prefix": "/m"' 'X-Watcher-Actor' 'RUNNING_EQUALS_FILE=yes' \
            'ENVFILE_ISOLATION ok trader-v3-controlplane-node-control' 'SYNC_CHECK_PENDING' 'UPSTREAM_URL: <value hidden' 'header_up X-Api-Key <literal len=' \
            "\"pattern\": \"$GOOD_RE\"" "@wgw_risk path_regexp $GOOD_RE" '"/hook/<seg len=40>/*"' 'handle /hook/<seg len=40>/*' \
            '"/<seg len=15>"' 'handle /<seg len=15>' '"uri": "/api/<seg len=37>"' 'rewrite /legacy/* /api/<seg len=37>' \
            'path_regexp ^/<seg len=46>/.*$' '"pattern": "^/<seg len=46>/.*$"' 'ExecStartPre=/usr/bin/curl -H Authorization: Bearer <redacted>' \
            '@r3a path_regexp ^/hook/<seg len=29>' '@r3b path_regexp ^/k/<seg len=26>' 'handle /k/<seg len=21>' 'handle /n/<seg len=16>' \
            'heartbeat_timeout_seconds=15 default_heartbeat_interval_seconds=2.0 node_py_overrides_interval=False' 'observed_max_hb_age account-a 1.1 samples=6' \
            'rewrite /t/* /t/<seg len=12>' 'root * /srv/<seg len=16>' '"/k/<seg len=12>/<seg len=12>"' '"pattern": "^/hook/<seg len=29>"'; do
  grep -qF -- "$want" "$OUT" || { echo "expected output missing: $want"; exit 1; }
done
# the full Caddy JSON never appears: no raw password value
if grep -qE '"password": "[^<]' "$OUT"; then echo "raw password field in output"; exit 1; fi
# parity: the site check's embedded skeleton == o0_tool.py caddy-skeleton on the same JSON
python3 - "$OUT" "$HERE/../o0_tool.py" "$ADAPTED_JSON" <<'PY' || { echo "skeleton parity failed"; exit 1; }
import json, subprocess, sys
text = open(sys.argv[1], encoding="utf-8").read()
start = text.index("-----BEGIN O0 ADAPTED FILE SKELETON JSON-----") + len("-----BEGIN O0 ADAPTED FILE SKELETON JSON-----")
end = text.index("-----END O0 ADAPTED FILE SKELETON JSON-----")
site = json.loads(text[start:end])
tool = json.loads(subprocess.run([sys.executable, sys.argv[2], "caddy-skeleton"], input=sys.argv[3], capture_output=True, text=True, check=True).stdout)
assert site == tool, "embedded skeleton differs from o0_tool.py caddy-skeleton"
PY
# parity of the embedded LINE rules too (review wac-032-r2 🟡-4): the site check's own
# redact_line / redact_path / caddy_skeleton against o0_tool.py on one corpus.
python3 - "$SITE" "$HERE/.." "$ADAPTED_JSON" <<'PY' || { echo "redaction parity failed"; exit 1; }
import json, re, sys
sys.path.insert(0, sys.argv[2])
import o0_tool
text = open(sys.argv[1], encoding="utf-8").read()
lib = re.search(r"<<'PY' \|\| \[ -n \"\$O0_PYLIB\" \]\n(.*?)\nPY\n", text, re.S).group(1)
site = {}
exec(lib, site)
corpus = [
    "Authorization: Bearer pw7short", "bearer\tpw7tab", "basic_auth balen pw7basic", "basicauth balen $2a$14$" + "x" * 53,
    "GET /x?token=pw7query&a=1", "RISK_ADMIN_TOKEN=pw7env", "export SOME_API_KEY: pw7colon", "Environment=A=pw7 B=pw7b",
    "handle /qctbhy0a32hucub {", "/hook/9f8e7d6c5b4a39281706f5e4d3c2b1a0ffeeddcc/*", "^/bot7654321098:AAHpathBOTsentinel0123456789abcd/.*$",
    "/api/SENTINELpathsecretXYZ0123456789abcdef", "^/m/v1/watcher/trading/risks/[^/]+$", "/m/v1/watcher/media/*",
    "token SENTINELfreeTOKEN0123456789abcdefXYZ here", "plain words only", "127.0.0.1:9090", "{env.WATCHER_BROWSER_PROXY_TOKEN}",
]
base_corpus = len(corpus)
corpus += [s for s, _ in o0_tool.PATH_SHAPES_R3] + [s for s, _ in o0_tool.PATH_RULE_PINS] + list(o0_tool.LEGIT_PATHS)
bad = []
for s in corpus:
    for name in ("redact_line", "redact_path"):
        a, b = getattr(o0_tool, name)(s), site[name](s)
        if a != b:
            bad.append(f"{name}({s[:30]!r}): tool={a!r} site={b!r}")
cfg = json.loads(sys.argv[3])
if o0_tool.caddy_skeleton(cfg) != site["caddy_skeleton"](cfg):
    bad.append("caddy_skeleton differs")
for s in corpus[:7] + corpus[8:12]:
    red = o0_tool.redact_line(s)
    for needle in ("pw7", "qctbhy", "9f8e7d", "AAHpath", "SENTINELpath"):
        if needle in red:
            bad.append(f"tool redact_line leaves {needle} in {s[:30]!r}")
for keep in corpus[12:14] + corpus[15:18]:
    if o0_tool.redact_line(keep) != keep:
        bad.append(f"over-redaction of {keep!r}")
# the site check's OWN copy, not only parity: every r3 shape 0 leaks in its line rule, path
# rule and skeleton; the hex/threshold pins exact; every legitimate path unchanged
for shape, needles in o0_tool.PATH_SHAPES_R3:
    cfg3 = {"apps": {"http": {"servers": {"s": {"routes": [{"match": [{"path": [shape]}, {"path_regexp": {"name": "n", "pattern": shape}}],
            "handle": [{"handler": "rewrite", "uri": shape, "strip_path_prefix": shape}, {"handler": "file_server", "root": shape}]}]}}}}}
    outs = [site["redact_path"](shape), json.dumps(site["caddy_skeleton"](cfg3))] + [site["redact_line"](f"\t{d} {shape}") for d in ("handle", "rewrite *", "@m path_regexp", "root *")]
    for o in outs:
        for n in needles:
            if n in o:
                bad.append(f"site copy leaks {n!r} for {shape!r}: {o[:80]!r}")
for shape, want in o0_tool.PATH_RULE_PINS:
    if site["redact_path"](shape) != want:
        bad.append(f"site redact_path pin {shape!r}: {site['redact_path'](shape)!r} != {want!r}")
for keep in o0_tool.LEGIT_PATHS:
    if site["redact_path"](keep) != keep or site["redact_line"](keep) != keep:
        bad.append(f"site copy over-redacts {keep!r}")
if bad:
    print("\n".join(bad))
    sys.exit(1)
print(f"REDACTION_PARITY_OK corpus={len(corpus)} (base {base_corpus} + r3 shapes {len(o0_tool.PATH_SHAPES_R3)} + pins {len(o0_tool.PATH_RULE_PINS)} + legit {len(o0_tool.LEGIT_PATHS)})")
PY
echo "LEAK_TEST_OK sections=$sections sentinels=${#SENTINELS[@]} leaks=0 skeleton_parity=ok redaction_parity=ok path_shapes=4+7"
