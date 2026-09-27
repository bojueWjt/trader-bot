const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const net = require("node:net");
const express = require("express");
const { Writable } = require("node:stream");
const { once } = require("node:events");
const { spawnSync } = require("node:child_process");
const Database = require("better-sqlite3");
const { createAuthMiddleware, isNeverAllowed, naIntersects, pathPart, assertPayload, neverAllowed } = require("../lib/auth");
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
    path: `/${filename}`,
    method,
    rawHeaders: Array.isArray(range) ? range.flatMap((value) => ["Range", value]) : (range ? ["Range", range] : []),
    get(name) { return name === "If-Range" ? ifRange : undefined; },
  };
  const authenticated = invoke('gateway', `/media/${filename}`, method).req;
  Object.defineProperty(req, 'watcherAuth', Object.getOwnPropertyDescriptor(authenticated, 'watcherAuth'));
  Object.defineProperty(req, 'watcherRoute', Object.getOwnPropertyDescriptor(authenticated, 'watcherRoute'));
  const res = new MediaResponse();
  const done = once(res, "finish");
  await createMediaHandler(root)(req, res);
  await done;
  return res;
}

test("never_allowed is the generated payload object", () => {
  const { PAYLOAD } = require("../lib/generated/gateway-routes");
  assert.equal(neverAllowed, PAYLOAD.never_allowed);
  assert.ok(neverAllowed.length > 0 && neverAllowed.every(row => row.methods === "*"));
});

test("watcher rejects malformed never_allowed at load time", () => {
  const { PAYLOAD } = require("../lib/generated/gateway-routes");
  const cases = [
    ["empty", (source) => { source.never_allowed = []; }, /never_allowed empty/u],
    ["shape", (source) => { source.never_allowed[0].reason = 3; }, /never_allowed shape/u],
    ["methods", (source) => { source.never_allowed[0].methods = ["GET"]; }, /never_allowed shape/u],
    ["path", (source) => { source.never_allowed[0].inner_path = "api/config"; }, /never_allowed path/u],
    ["duplicate", (source) => { source.never_allowed.push({ ...source.never_allowed[0] }); }, /never_allowed path/u],
    ["browser intersection", (source) => { source.never_allowed[0].inner_path = "/api/not-mounted"; }, /browser intersection/u],
  ];
  for (const [name, change, pattern] of cases) {
    const source = structuredClone(PAYLOAD);
    change(source);
    assert.throws(() => assertPayload(source), pattern, name);
    assert.throws(() => createAuthMiddleware(TOKENS, source), pattern, name);
  }
});

test("non-ASCII and control characters fail before route matching", () => {
  for (const identity of ["gateway", "snapshot"]) {
    for (const character of ["\u00a0", "\u3000", "é", "\x7f", "\x1f"]) {
      const result = invoke(identity, `/api/status${character}`);
      assert.equal(result.res.statusCode, 403, `${identity} U+${character.codePointAt(0).toString(16)}`);
      assert.equal(result.res.body.code, "identity_forbidden");
      assert.equal(result.passed, false);
    }
  }
});

test("percent normalization rejects a path assigned to another identity", () => {
  const result = invoke("gateway", "/api/trading/accounts/acct-1%23x");
  assert.equal(result.res.statusCode, 404);
  assert.equal(result.res.body.code, "route_not_found");
  assert.equal(result.passed, false);
});

test("NA, path_part and intersection fixed probes", () => {
  for (const pathname of ["/api/config", "/API/CONFIG", "/api/config/", "/api/login", "/api/login/", "/api/login/start", "/api/login/anything/else", "/api/login/qr/status", "/", "//", "/index.html", "/healthz", "/healthz/"]) {
    assert.equal(isNeverAllowed(pathname), true, pathname);
  }
  for (const pathname of ["/api/configs", "/api/loginx", "/api/login-x", "/api/status", "/index.htm", "/healthzz", "/media/1-1.jpg"]) {
    assert.equal(isNeverAllowed(pathname), false, pathname);
  }
  for (const pathname of ["/api/login/{x}", "/{x}", "/api/{x}", "/api/{x}/start"]) {
    assert.ok(neverAllowed.some((entry) => naIntersects(pathname, entry)), pathname);
  }
  for (const pathname of ["/api/status", "/api/trading/{x}", "/media/{filename}", "/api/price-alerts/{alert_id}"]) {
    assert.ok(!neverAllowed.some((entry) => naIntersects(pathname, entry)), pathname);
  }
  for (const [target, expected] of [["/api/config#x", "/api/config"], ["/api/config?a#b", "/api/config"], ["/api/status#/../config", "/api/status"], ["/#x", "/"], ["/healthz?a", "/healthz"], ["/api/status", "/api/status"], ["http://x/api/config", "http://x/api/config"]]) {
    assert.equal(pathPart(target), expected);
  }
  const { PAYLOAD } = require("../lib/generated/gateway-routes");
  const injected = structuredClone(PAYLOAD);
  injected.routes.push({ ...injected.routes.find((row) => row.identity === "gateway"), inner_path: "/api/login/x" });
  assert.throws(() => assertPayload(injected), /intersection/u);
  assert.throws(() => createAuthMiddleware(TOKENS, injected), /intersection/u);
  assert.equal(invoke("gateway", "/").res.statusCode, 403);
  assert.equal(invoke("snapshot", "/api/config").res.statusCode, 403);
  assert.equal(invoke("gateway", "/api/login%2Fstart").res.statusCode, 403);
  assert.equal(invoke("gateway", "/api/trading/accounts/%E0%A4%A").res.statusCode, 403);
  assert.notEqual(invoke("browser", "/").res.statusCode, 403);
  assert.equal(invoke("browser", "/healthz").passed, true);
  for (const identity of ["gateway", "snapshot"]) {
    for (const method of ["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]) {
      for (const pathname of ["/api/config", "/API/CONFIG", "/api/login", "/api/login/anything", "/api/login%2Fstart", "/", "/index.html", "/healthz", "http://x/api/status", "*", "/api\\status", "/api/status\""]) {
        const result = invoke(identity, pathname, method);
        assert.equal(result.res.statusCode, 403, `${identity} ${method} ${pathname}`);
        assert.equal(result.passed, false);
      }
      for (const pathname of ["/api/status#x", "//index.html", "/./index.html", "/%2e/index.html", "/%2E/", "/%2Findex.html", "/.%2findex.html", "/x/%2e%2e/index.html"]) {
        const result = invoke(identity, pathname, method);
        assert.equal(result.res.statusCode, 404, `${identity} ${method} ${pathname}`);
        assert.equal(result.passed, false);
      }
    }
  }
});

test("dump-route-table matches generated identities and registered handlers", () => {
  const script = path.resolve(__dirname, "../scripts/dump-route-table.js");
  const result = spawnSync(process.execPath, [script], { encoding: "utf8", timeout: 15000 });
  assert.equal(result.status, 0, result.stderr);
  const actual = JSON.parse(result.stdout);
  const { PAYLOAD } = require("../lib/generated/gateway-routes");
  assert.deepEqual(actual.identity_routes, PAYLOAD.routes.map(({ identity, method, inner_path }) => ({ identity, method, inner_path })));
  assert.ok(actual.handlers.length > 0);
  assert.deepEqual(actual.missing, []);
  assert.deepEqual(actual.unexpected, []);
});

test("dump-route-table reports rogue literal, parameter and mounted handlers", () => {
  const script = path.resolve(__dirname, "../scripts/dump-route-table.js");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "watcher-route-injection-"));
  const preload = path.join(root, "inject.js");
  try {
    for (const [route, registration] of [
      ["/api/rogue", "post"],
      ["/api/:rogue", "post"],
      ["/api/rogue-middleware", "use"],
    ]) {
      fs.writeFileSync(preload, `const Module = require("node:module");\nconst load = Module._load;\nModule._load = function(request, parent, main) {\n  const result = load.apply(this, arguments);\n  if (request === "../server") result.app.${registration}(${JSON.stringify(route)}, (_, res) => res.json({}));\n  return result;\n};\n`);
      const result = spawnSync(process.execPath, ["--require", preload, script], { encoding: "utf8", timeout: 15000 });
      assert.equal(result.status, 1, `${route}: ${result.stderr}`);
      const actual = JSON.parse(result.stdout);
      assert.ok(actual.unexpected.some((row) => row.path === route), route);
    }
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("dump-route-table rejects unexpected mounts and routes in every Express form", () => {
  const script = path.resolve(__dirname, "../scripts/dump-route-table.js");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "watcher-route-injection-"));
  const preload = path.join(root, "inject.js");
  const inject = [
    ["api root mount", 'result.app.use("/api", (_, res) => res.end());'],
    ["media impostor", 'result.app.use("/media", function mediaHandler(_, res) { res.end(); });'],
    ["mixed case mount", 'result.app.use("/API/x", (_, res) => res.end());'],
    ["upper case route", 'result.app.post("/API/rogue", (_, res) => res.end());'],
    ["mixed case route", 'result.app.post("/Api/Rogue", (_, res) => res.end());'],
    ["array mount", 'result.app.use(["/api/x"], (_, res) => res.end());'],
    ["regex mount", 'result.app.use(/^\\/api\\/x/, (_, res) => res.end());'],
    ["root child router", `const router = require(${JSON.stringify(require.resolve("express"))}).Router(); router.post("/api/rogue", (_, res) => res.end()); result.app.use(router);`],
    ["media name on api", 'result.app.use("/api/x", function mediaHandler(_, res) { res.end(); });'],
    ["rogue root mount", 'result.app.use((_, res) => res.end());'],
  ];
  try {
    for (const [label, registration] of inject) {
      fs.writeFileSync(preload, `const Module = require("node:module");\nconst load = Module._load;\nModule._load = function(request, parent, main) {\n  const result = load.apply(this, arguments);\n  if (request === "../server") { ${registration} }\n  return result;\n};\n`);
      const result = spawnSync(process.execPath, ["--require", preload, script], { encoding: "utf8", timeout: 15000 });
      assert.equal(result.status, 1, `${label}: ${result.stderr}`);
      const actual = JSON.parse(result.stdout);
      assert.ok(actual.unexpected.length > 0, label);
      if (label === "root child router") {
        assert.ok(actual.unexpected.some((row) => row.method === "POST" && row.path === "/api/rogue"), label);
      }
    }
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("non browser raw targets are rejected before handler and static through real sockets", async () => {
  const app = express();
  let handlers = 0;
  let statics = 0;
  app.use(createAuthMiddleware(TOKENS));
  app.use((req, res, next) => { handlers += 1; next(); });
  app.use((req, res, next) => { statics += 1; next(); });
  const server = app.listen(0, "127.0.0.1");
  await once(server, "listening");
  const port = server.address().port;
  async function rawRequest(identity, method, target) {
    return new Promise((resolve, reject) => {
      const socket = net.createConnection({ host: "127.0.0.1", port });
      const chunks = [];
      socket.on("connect", () => {
        const token = identity === "gateway" ? TOKENS.WATCHER_GATEWAY_TOKEN : TOKENS.WATCHER_SNAPSHOT_TOKEN;
        socket.write(`${method} ${target} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer ${token}\r\nConnection: close\r\n\r\n`);
      });
      socket.on("data", (chunk) => chunks.push(chunk));
      socket.on("error", reject);
      socket.on("end", () => resolve(Buffer.concat(chunks).toString()));
    });
  }
  try {
    const forbidden = ["/api/config#x", "/api/login/start#x", "/healthz#x", "/#x", "/index.html#x", "http://x/api/config", "http://x/api/status", "*", "/api\\config#x", "/api\\status", '/api/status"', "/api/{x}", "//", "/api/login/anything", "/api/login%2Fstart"];
    const missing = ["/api/status#x", "//index.html", "/./index.html", "///", "/%2e/index.html", "/%2E/", "/%2Findex.html", "/.%2findex.html", "/x/%2e%2e/index.html"];
    for (const identity of ["gateway", "snapshot"]) {
      for (const method of ["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]) {
        for (const [target, status] of [...forbidden.map((item) => [item, 403]), ...missing.map((item) => [item, 404])]) {
          const response = await rawRequest(identity, method, target);
          assert.match(response, new RegExp(`^HTTP/1\\.1 ${status} `), `${identity} ${method} ${target}`);
          if (method !== "HEAD") {
            assert.match(response, status === 403 ? /identity_forbidden/u : /route_not_found/u);
          }
          assert.equal(handlers, 0);
          assert.equal(statics, 0);
        }
      }
    }
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
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
  assert.equal(fs.existsSync(path.join(path.dirname(serverPath), "config.json")), false, "startup test must not read local Telegram config");
  const startup = spawnSync(process.execPath, [serverPath], { env: clean, encoding: "utf8", timeout: 5000 });
  assert.notEqual(startup.status, 0);
  assert.match(startup.stderr, /WATCHER_GATEWAY_TOKEN/);
});

test("every generated route admits its own identity and method", () => {
  const { PAYLOAD: { routes } } = require("../lib/generated/gateway-routes");
  const examples = {
    account_id: "acct-1",
    channel_id: "-123",
    symbol: "BTCUSDT",
    filename: "1234567890-1.jpg",
    alert_id: "1",
    order_id: "1",
  };
  assert.ok(routes.length > 0);
  for (const route of routes) {
    const pathname = route.inner_path.replace(/\{([a-z_]+)\}/g, (_, name) => examples[name]);
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

test("server real Express stack gates all handlers and static content", () => {
  assert.equal(fs.existsSync(path.resolve(__dirname, "../config.json")), false);
  const previous = { ...process.env };
  Object.assign(process.env, TOKENS);
  const mediaDir = fs.mkdtempSync(path.join(os.tmpdir(), "watcher-stack-"));
  process.env.WATCHER_MEDIA_DIR = mediaDir;
  const listeners = new Map(['uncaughtException','unhandledRejection','SIGINT','SIGTERM'].map(name => [name,process.listeners(name)]));
  try {
    const { app } = require('../server');
    const stack = app.router.stack;
    assert.equal(stack[0].name, 'watcherAuth');
    assert.equal(stack[1].name, 'watcherRequestValidation');
    assert.ok(stack.some(layer => layer.name === 'serveStatic'));
    assert.ok(stack.some(layer => layer.route));
    for (const layer of stack.filter(layer => layer.route || layer.name === 'serveStatic' || layer.name === 'mediaHandler')) {
      assert.ok(stack.indexOf(layer) > 1);
    }
  } finally {
    for (const name of Object.keys(process.env)) {
      if (!(name in previous)) delete process.env[name];
    }
    Object.assign(process.env, previous);
    for (const [name, old] of listeners) {
      for (const listener of process.listeners(name)) {
        if (!old.includes(listener)) process.removeListener(name, listener);
      }
    }
    fs.rmSync(mediaDir, { recursive: true, force: true });
  }
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

test("status distinguishes an unreadable database from a readable empty table", () => {
  const state = { connected: true, listening: true, now: 1000, lastTelegramActivityAt: 500 };
  assert.throws(() => buildStatus({}, state, { TRADER_TRADING_DB_PATH: "/does-not-exist" }), /unreadable/);
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'watcher-empty-status-'));
  const file = path.join(directory, 'db');
  const db = new Database(file);
  db.exec('CREATE TABLE telegram_messages (created_at TEXT)');
  db.close();
  try {
    const value = buildStatus({ apiId: '1', apiHash: 'fake', session: 'fake', watchGroups: ['42'] }, state, { TRADER_TRADING_DB_PATH: file });
    assert.deepEqual([value.configured, value.loggedIn, value.connected, value.watchGroups], [true,true,true,['42']]);
    assert.equal(value.last_message_ingested_at, null);
    assert.equal(value.last_telegram_activity_at, '1970-01-01T00:00:00.500Z');
  } finally { fs.rmSync(directory, { recursive:true, force:true }); }
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

test('media client cancellation logs bytes_sent and closes the file stream', async () => {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'watcher-media-cancel-'));
  const filename='1234567890-99.jpg';
  fs.writeFileSync(path.join(root,filename),Buffer.alloc(256*1024));
  const logs=[];
  const original=console.error;
  console.error=(line)=>logs.push(line);
  class CancelResponse extends MediaResponse {
    _write(chunk, encoding, callback) {
      this.headersSent=true;
      callback();
      this.destroy();
    }
  }
  try {
    const req=invoke('gateway','/media/'+filename).req;
    req.path='/'+filename;req.get=()=>undefined;
    const res=new CancelResponse();
    const closed=once(res,'close');
    await createMediaHandler(root)(req,res);await closed;
    assert.equal(logs.length,1);
    assert.match(logs[0],/truncated filename=1234567890-99.jpg bytes_sent=65536 reason=client_closed/);
    const missing=new MediaResponse();
    await createMediaHandler(root)({path:'/'+filename},missing);
    assert.equal(missing.statusCode,500);
  } finally {console.error=original;fs.rmSync(root,{recursive:true,force:true});}
});
