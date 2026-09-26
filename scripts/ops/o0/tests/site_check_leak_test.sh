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

ADAPTED_JSON="{\"apps\":{\"http\":{\"servers\":{\"srv0\":{\"listen\":[\":443\"],\"routes\":[{\"match\":[{\"header\":{\"Authorization\":[\"Bearer $S7\"]},\"path\":[\"/hook\"]}],\"handle\":[{\"handler\":\"headers\",\"request\":{\"replace\":{\"X-Up\":[{\"search\":\"a\",\"replace\":\"$S8\"}]},\"delete\":[\"X-Watcher-Actor\"]}},{\"handler\":\"static_response\",\"status_code\":200,\"body\":\"$S9\"}]},{\"handle\":[{\"handler\":\"vars\",\"k\":\"$S10\"},{\"handler\":\"rewrite\",\"strip_path_prefix\":\"/m\",\"uri\":\"/x?token=$S11\"},{\"handler\":\"authentication\",\"providers\":{\"http_basic\":{\"accounts\":[{\"username\":\"balen\",\"password\":\"$S3\"}]}}},{\"handler\":\"reverse_proxy\",\"headers\":{\"request\":{\"set\":{\"Authorization\":[\"Bearer $S4\"],\"X-Other\":[\"$S10\"],\"X-Watcher-Proxy-Auth\":[\"{env.WATCHER_BROWSER_PROXY_TOKEN}\"]}}},\"upstreams\":[{\"dial\":\"127.0.0.1:9090\"}]}]}]}}}}}"

stub() { printf '#!/usr/bin/env bash\n%s\n' "$2" > "$STUB/$1"; chmod +x "$STUB/$1"; }
stub id 'echo 0'
stub docker "case \"\$*\" in
  *psql*) echo 'account-a ACTIVE abcdef012345 hb_age=1.1';;
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
  cat*) printf '%s\n' 'ExecStart=/srv/trader-v3/.venv-cp/bin/uvicorn read_api:app --workers 2' 'Environment=FEEDER_TOKEN=$S6' 'EnvironmentFile=$WORK/oq.env';;
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
  WATCHER_ROOT="$WORK/trader" WATCHER_DB="$WORK/watcher.db" REPLICA_DB="$WORK/replica.db" VENV_PY=python3 \
  bash -s -- --execute --auth-id O0-A01 < "$SITE" > "$OUT" 2>&1 || true

[ -z "${O0_KEEP_OUT:-}" ] || cp "$OUT" "$O0_KEEP_OUT"
sections=$(grep -c '^=== S-' "$OUT" || true)
leaks=0
SENTINELS=("$S1" "$S2" "$S3" "$S4" "$S5" "$S6" "$S7" "$S8" "$S9" "$S10" "$S11" SENTINELdbKEY SENTINELdbSECRET SENTINELbcrypt pw7)
for s in "${SENTINELS[@]}"; do
  if grep -qF -- "$s" "$OUT"; then echo "LEAK ${s:0:18}..." ; leaks=$((leaks + 1)); fi
done
grep -q 'O0_SITE_CHECK_DONE' "$OUT" || { echo "site check did not finish"; tail -20 "$OUT"; exit 1; }
if [ "$sections" -lt 19 ]; then echo "only $sections sections ran"; exit 1; fi
if [ "$leaks" -gt 0 ]; then echo "LEAK_TEST_FAILED leaks=$leaks"; exit 1; fi
grep -q '<literal len=' "$OUT" || { echo "literal header was not flagged"; exit 1; }
# the skeleton keeps what the checklist needs (structure, placeholders, dials)
for want in '"dial": "127.0.0.1:9090"' '{env.WATCHER_BROWSER_PROXY_TOKEN}' '"strip_path_prefix": "/m"' 'X-Watcher-Actor' 'RUNNING_EQUALS_FILE=yes' \
            'ENVFILE_ISOLATION ok trader-v3-controlplane-node-control' 'SYNC_CHECK_PENDING' 'UPSTREAM_URL: <value hidden' 'header_up X-Api-Key <literal len='; do
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
echo "LEAK_TEST_OK sections=$sections sentinels=${#SENTINELS[@]} leaks=0 skeleton_parity=ok"
