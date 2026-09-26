const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { Writable } = require("node:stream");
const { once } = require("node:events");
const { spawnSync } = require("node:child_process");
const Database = require("better-sqlite3");
const { createAuthMiddleware } = require("../lib/auth");
const { createMediaHandler, parseRange } = require("../lib/media");
const { buildStatus } = require("../lib/status");

const TOKENS = {
  WATCHER_GATEWAY_TOKEN: "g".repeat(40),
  WATCHER_SNAPSHOT_TOKEN: "s".repeat(40),
  WATCHER_BROWSER_PROXY_TOKEN: "b".repeat(40),
};

function headers(identity, actor = "app:viewer") {
  if (identity === "browser") {
    return ["X-Watcher-Proxy-Auth", TOKENS.WATCHER_BROWSER_PROXY_TOKEN];
  }
  if (identity === "snapshot") {
    return ["Authorization", `Bearer ${TOKENS.WATCHER_SNAPSHOT_TOKEN}`];
  }
  return ["Authorization", `Bearer ${TOKENS.WATCHER_GATEWAY_TOKEN}`, "X-Watcher-Actor", actor, "X-Watcher-Token-Fingerprint", "012345abcdef"];
}

function response() {
  return {
    statusCode: 200,
    headers: {},
    body: undefined,
    setHeader(name, value) { this.headers[name.toLowerCase()] = value; },
    status(value) { this.statusCode = value; return this; },
    json(value) { this.body = value; return this; },
  };
}

function invoke(identity, pathname, method = "GET", extra = [], env = TOKENS) {
  const req = { rawHeaders: [...(identity ? headers(identity, method === "GET" ? "app:viewer" : "app:risk_admin") : []), ...extra], originalUrl: pathname, method };
  const res = response();
  let passed = false;
  createAuthMiddleware(env)(req, res, () => { passed = true; });
  return { req, res, passed };
}

class MediaResponse extends Writable {
  constructor() {
    super();
    this.statusCode = 200;
    this.headers = {};
    this.chunks = [];
    this.headersSent = false;
  }
  _write(chunk, encoding, callback) {
    this.headersSent = true;
    this.chunks.push(Buffer.from(chunk));
    callback();
  }
  setHeader(name, value) { this.headers[name.toLowerCase()] = value; }
  status(value) { this.statusCode = value; return this; }
  json(value) { this.end(JSON.stringify(value)); return this; }
  get text() { return Buffer.concat(this.chunks).toString(); }
}

async function mediaRequest(root, filename, method = "GET", range, ifRange) {
  const req = {
    watcherAuth: { identity: "gateway" },
    path: `/${filename}`,
    method,
    rawHeaders: Array.isArray(range) ? range.flatMap((value) => ["Range", value]) : (range ? ["Range", range] : []),
    get(name) { return name === "If-Range" ? ifRange : undefined; },
  };
  const res = new MediaResponse();
  const done = once(res, "finish");
  await createMediaHandler(root)(req, res);
  await done;
  return res;
}

test("route artifact has exactly the YAML identity, method, path and write rows", () => {
  const yamlPath = path.resolve(__dirname, "../../../../contracts/watcher-gateway-routes.yaml");
  const python = "import json,sys,yaml; d=yaml.safe_load(open(sys.argv[1])); print(json.dumps([[r['id'],r['identity'],r['method'],r['inner_path'],bool(r['write'])] for r in d['routes']]))";
  const result = spawnSync("python3", ["-c", python, yamlPath], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
  const expected = JSON.parse(result.stdout);
  assert.ok(expected.length > 0);
  const { routes, pathParams, actorHeaders, neverAllowed, secretKeyPattern } = require("../lib/generated/watcher-routes");
  assert.deepEqual(routes.map((row) => [row.id, row.identity, row.method, row.path, row.write]), expected);
  const metadata = spawnSync("python3", ["-c", "import json,sys,yaml;d=yaml.safe_load(open(sys.argv[1]));print(json.dumps([d['path_params'],d['actor_headers'],[r['inner_path'] for r in d['never_allowed']]],ensure_ascii=False))", yamlPath], { encoding: "utf8" });
  assert.equal(metadata.status, 0, metadata.stderr);
  assert.deepEqual([pathParams, actorHeaders, neverAllowed], JSON.parse(metadata.stdout));
  assert.equal(secretKeyPattern.flags, "i");
  assert.equal(secretKeyPattern.pattern.startsWith("(?i)"), false);
});

test("startup validates all current and configured previous tokens", () => {
  assert.throws(() => createAuthMiddleware({}), /WATCHER_/);
  assert.throws(() => createAuthMiddleware({ ...TOKENS, WATCHER_GATEWAY_TOKEN_PREVIOUS: "p".repeat(31) }), /WATCHER_/);
  assert.throws(() => createAuthMiddleware({ ...TOKENS, WATCHER_GATEWAY_TOKEN_PREVIOUS: TOKENS.WATCHER_SNAPSHOT_TOKEN }), /WATCHER_/);
  assert.doesNotThrow(() => createAuthMiddleware({ ...TOKENS, WATCHER_GATEWAY_TOKEN_PREVIOUS: "p".repeat(40) }));
  const clean = { ...process.env };
  for (const name of Object.keys(TOKENS)) {
    delete clean[name];
    delete clean[`${name}_PREVIOUS`];
  }
  const serverPath = path.resolve(__dirname, "../server.js");
  const startup = spawnSync(process.execPath, [serverPath], { env: clean, encoding: "utf8", timeout: 5000 });
  assert.notEqual(startup.status, 0);
  assert.match(startup.stderr, /WATCHER_GATEWAY_TOKEN/);
});

test("every generated route admits its own identity and method", () => {
  const { routes } = require("../lib/generated/watcher-routes");
  const examples = {
    account_id: "acct-1",
    channel_id: "-123",
    symbol: "BTCUSDT",
    filename: "1234567890-1.jpg",
    alert_id: "1",
    order_id: "1",
  };
  assert.ok(routes.length >= 60);
  for (const route of routes) {
    const pathname = route.path.replace(/\{([a-z_]+)\}/g, (_, name) => examples[name]);
    const actor = route.write ? "app:risk_admin" : "app:viewer";
    const supplied = headers(route.identity, actor);
    const result = invoke(null, pathname, route.method, supplied);
    assert.equal(result.passed, true, `${route.id} ${route.method} ${pathname}: ${result.res.statusCode}`);
    assert.equal(result.req.watcherAuth.identity, route.identity);
  }
});

test("missing, wrong, empty and duplicate entrance headers fail closed", () => {
  assert.equal(invoke(null, "/api/status").res.statusCode, 401);
  assert.equal(invoke(null, "/api/status", "GET", ["Authorization", "Bearer wrong"]).res.statusCode, 401);
  for (const value of ["Bearer ", "Bearer    "]) {
    assert.equal(invoke(null, "/api/status", "GET", ["Authorization", value]).res.statusCode, 401);
  }
  assert.equal(invoke(null, "/api/status", "GET", ["X-Watcher-Proxy-Auth", "  "]).res.statusCode, 401);
  assert.equal(invoke("gateway", "/api/status", "GET", ["Authorization", `Bearer ${TOKENS.WATCHER_GATEWAY_TOKEN}`]).res.statusCode, 401);
  assert.equal(invoke("browser", "/api/status", "GET", ["X-Watcher-Proxy-Auth", TOKENS.WATCHER_BROWSER_PROXY_TOKEN]).res.statusCode, 401);
  assert.equal(invoke("gateway", "/api/status", "GET", ["X-Watcher-Proxy-Auth", TOKENS.WATCHER_BROWSER_PROXY_TOKEN]).res.statusCode, 401);
  assert.equal(invoke(null, "/healthz", "GET", ["Authorization", "Basic ignored", ...headers("browser")]).passed, true);
});

test("gateway, snapshot and browser access only their routes", () => {
  const gateway = invoke("gateway", "/api/status");
  assert.equal(gateway.passed, true);
  assert.deepEqual(gateway.req.watcherAuth, { identity: "gateway", role: "viewer", actor: "app:viewer", tokenFingerprint: "012345abcdef" });
  assert.equal(invoke("snapshot", "/api/trading/config-snapshot").passed, true);
  assert.equal(invoke("browser", "/healthz").passed, true);
  assert.equal(invoke("browser", "/api/status").passed, true);
  assert.equal(invoke("gateway", "/healthz").res.statusCode, 403);
  assert.equal(invoke("snapshot", "/api/status").res.statusCode, 403);
  assert.equal(invoke("browser", "/api/trading/config-snapshot").res.statusCode, 403);
  assert.equal(invoke("gateway", "/api/config", "GET").res.statusCode, 403);
  assert.equal(invoke("gateway", "/api/config", "POST").res.statusCode, 403);
  assert.equal(invoke("gateway", "/api/config", "DELETE").res.statusCode, 403);
  assert.equal(invoke("gateway", "/api/login/start", "DELETE").res.statusCode, 403);
  assert.equal(invoke("gateway", "/api/price-alerts/order/1", "DELETE").res.statusCode, 403);
  assert.equal(invoke("gateway", "/api/status/").res.statusCode, 404);
  assert.equal(invoke("gateway", "/api/%73tatus").res.statusCode, 404);
  assert.equal(invoke("gateway", "/api/status", "HEAD").res.statusCode, 405);
  assert.equal(invoke("gateway", "/api/status", "HEAD").res.headers.allow, "GET");
  assert.equal(invoke(null, "/healthz").res.statusCode, 401);
  const browserEncoded = invoke(null, "/api/trading/accounts/user%40example.com", "PUT", headers("browser"));
  assert.equal(browserEncoded.passed, true);
});

test("server wires authentication ahead of JSON, static content, media and handlers", () => {
  const source = fs.readFileSync(path.resolve(__dirname, "../server.js"), "utf8");
  const positions = [
    source.indexOf("app.use(watcherAuthMiddleware)"),
    source.indexOf("app.use(express.json())"),
    source.indexOf("app.use(express.static"),
    source.indexOf("app.use(\"/media\", createMediaHandler"),
    source.indexOf("app.get(\"/healthz\""),
  ];
  assert.ok(positions.every((value) => value >= 0));
  assert.deepEqual(positions, [...positions].sort((a, b) => a - b));
  assert.equal(source.includes("express.static(MEDIA_DIR)"), false);
});

test("actor and fingerprint headers are mandatory, unique and gateway only", () => {
  const base = headers("gateway");
  assert.equal(invoke(null, "/api/status", "GET", base.slice(0, 2)).res.statusCode, 400);
  assert.equal(invoke(null, "/api/status", "GET", [...base.slice(0, 4)]).res.statusCode, 400);
  assert.equal(invoke("gateway", "/api/status", "GET", ["X-Watcher-Actor", "app:viewer"]).res.statusCode, 400);
  assert.equal(invoke("gateway", "/api/status", "GET", ["X-Watcher-Token-Fingerprint", "012345abcdef"]).res.statusCode, 400);
  assert.equal(invoke("browser", "/api/status", "GET", ["X-Watcher-Actor", "app:risk_admin"]).res.statusCode, 400);
  assert.equal(invoke("snapshot", "/api/trading/config-snapshot", "GET", ["X-Watcher-Token-Fingerprint", "012345abcdef"]).res.statusCode, 400);
  assert.equal(invoke(null, "/api/groups", "POST", headers("gateway", "app:viewer")).res.statusCode, 403);
  assert.equal(invoke(null, "/api/groups", "POST", headers("gateway", "app:risk_admin")).passed, true);
});

test("rotation accepts previous, revocation rejects it, responses omit token sentinels", () => {
  const previous = "p".repeat(40);
  const header = ["Authorization", `Bearer ${previous}`, "X-Watcher-Actor", "app:viewer", "X-Watcher-Token-Fingerprint", "012345abcdef"];
  const rotated = invoke(null, "/api/status", "GET", header, { ...TOKENS, WATCHER_GATEWAY_TOKEN_PREVIOUS: previous });
  assert.equal(rotated.passed, true);
  const revoked = invoke(null, "/api/status", "GET", header);
  assert.equal(revoked.res.statusCode, 401);
  const serialized = JSON.stringify(revoked.res.body);
  for (const value of Object.values(TOKENS).concat(previous)) {
    assert.equal(serialized.includes(value), false);
  }
});

test("authentication failures do not log or return presented token sentinels", () => {
  const sentinel = "secret-sentinel-".repeat(3);
  const captured = [];
  const oldLog = console.log;
  const oldError = console.error;
  console.log = (...parts) => captured.push(parts.join(" "));
  console.error = (...parts) => captured.push(parts.join(" "));
  let failure;
  try {
    failure = invoke(null, "/api/status", "GET", ["Authorization", `Bearer ${sentinel}`]);
  } finally {
    console.log = oldLog;
    console.error = oldError;
  }
  assert.equal(failure.res.statusCode, 401);
  assert.equal(JSON.stringify(failure.res.body).includes(sentinel), false);
  assert.equal(captured.join(" ").includes(sentinel), false);
});

test("compose healthcheck obtains browser token inside node and keeps response silent", () => {
  const yamlPath = path.resolve(__dirname, "../../../docker-compose.yml");
  const script = spawnSync("python3", ["-c", "import yaml,sys;d=yaml.safe_load(open(sys.argv[1]));print(d['services']['watcher']['healthcheck']['test'][-1])", yamlPath], { encoding: "utf8" });
  assert.equal(script.status, 0, script.stderr);
  assert.match(script.stdout, /process\.env\.WATCHER_BROWSER_PROXY_TOKEN/);
  assert.match(script.stdout, /X-Watcher-Proxy-Auth/);
  assert.equal(script.stdout.includes("${WATCHER_BROWSER_PROXY_TOKEN}"), false);
  assert.equal(script.stdout.includes("console."), false);
});

test("media handles full, open, suffix and one-byte ranges, HEAD and invalid ranges", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "watcher-media-test-"));
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), "watcher-outside-test-"));
  const filename = "1234567890-1.jpg";
  fs.writeFileSync(path.join(root, filename), "abcdef");
  fs.writeFileSync(path.join(outside, filename), "secret");
  fs.symlinkSync(path.join(outside, filename), path.join(root, "1234567890-2.jpg"));
  try {
    const full = await mediaRequest(root, filename);
    assert.equal(full.statusCode, 200);
    assert.equal(full.text, "abcdef");
    assert.equal(full.headers["cache-control"], "private, no-store");
    assert.equal(full.headers["content-length"], "6");
    assert.equal(full.headers["content-type"], "image/jpeg");
    const one = await mediaRequest(root, filename, "GET", "bytes=0-0");
    assert.equal(one.statusCode, 206);
    assert.equal(one.headers["content-range"], "bytes 0-0/6");
    assert.equal(one.text, "a");
    assert.equal((await mediaRequest(root, filename, "GET", "bytes=3-")).text, "def");
    assert.equal((await mediaRequest(root, filename, "GET", "bytes=-2")).text, "ef");
    const head = await mediaRequest(root, filename, "HEAD", "bytes=0-0");
    assert.equal(head.statusCode, 200);
    assert.equal(head.headers["content-length"], "6");
    assert.equal(head.text, "");
    const mismatch = await mediaRequest(root, filename, "GET", "bytes=0-0", "different-etag");
    assert.equal(mismatch.statusCode, 200);
    assert.equal(mismatch.text, "abcdef");
    const match = await mediaRequest(root, filename, "GET", "bytes=0-0", full.headers.etag);
    assert.equal(match.statusCode, 206);
    assert.equal(match.text, "a");
    for (const range of ["bytes=0-1,3-4", "items=0-1", "bytes=3-1", "bytes=9-", "bytes=-0", ["bytes=0-0", "bytes=1-1"]]) {
      const invalid = await mediaRequest(root, filename, "GET", range);
      assert.equal(invalid.statusCode, 416, range);
      assert.equal(invalid.headers["content-range"], "bytes */6");
    }
    assert.equal((await mediaRequest(root, "1234567890-2.jpg")).statusCode, 404);
    fs.writeFileSync(path.join(root, "1234567890-3.jpg"), "");
    fs.truncateSync(path.join(root, "1234567890-3.jpg"), 20 * 1024 * 1024 + 1);
    const tooLarge = await mediaRequest(root, "1234567890-3.jpg");
    assert.equal(tooLarge.statusCode, 503);
    assert.equal(JSON.parse(tooLarge.text).code, "media_too_large");
    const allowedHeaders = new Set(["content-range", "accept-ranges", "content-length", "content-type", "etag", "last-modified", "cache-control"]);
    for (const name of Object.keys(full.headers)) {
      assert.equal(allowedHeaders.has(name), true, name);
    }
    assert.deepEqual(parseRange("bytes=-2", 6), { start: 4, end: 5 });
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
    fs.rmSync(outside, { recursive: true, force: true });
  }
});

test("status adds activity and listener fields while retaining original fields", () => {
  const value = buildStatus({ apiId: "1", apiHash: "fake", session: "fake", watchGroups: ["42"] }, { connected: true, listening: true, now: 1000, lastTelegramActivityAt: 500 }, { TRADER_TRADING_DB_PATH: "/does-not-exist" });
  assert.deepEqual([value.configured, value.loggedIn, value.connected, value.watchGroups], [true, true, true, ["42"]]);
  assert.equal(value.observed_at, "1970-01-01T00:00:01.000Z");
  assert.equal(value.connection, "connected");
  assert.equal(value.listener, "listening");
  assert.equal(value.last_telegram_activity_at, "1970-01-01T00:00:00.500Z");
  assert.equal(value.last_message_ingested_at, null);
});

test("status reads the latest ingested message timestamp from a local fixture DB", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "watcher-status-test-"));
  const dbPath = path.join(root, "messages.db");
  const db = new Database(dbPath);
  try {
    db.exec("CREATE TABLE telegram_messages (created_at TEXT)");
    db.prepare("INSERT INTO telegram_messages (created_at) VALUES (?)").run("2026-09-26 03:00:00");
  } finally {
    db.close();
  }
  try {
    const value = buildStatus({}, { connected: false, listening: false, now: 1000, lastTelegramActivityAt: null }, { TRADER_TRADING_DB_PATH: dbPath });
    assert.equal(value.connection, "needs_login");
    assert.equal(value.listener, "stopped");
    assert.equal(value.last_telegram_activity_at, null);
    assert.equal(value.last_message_ingested_at, "2026-09-26T03:00:00.000Z");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
