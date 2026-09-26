const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { spawn } = require("node:child_process");
const Database = require("better-sqlite3");
const express = require("express");

const temp = fs.mkdtempSync(path.join(os.tmpdir(), "watcher-config-"));
const file = path.join(temp, "trading.db");
const tradingApiModule = require.resolve("../lib/trading-api");
let server;
let base;
let counter = 0;
let revision = 0;

function database() {
  const db = new Database(file);
  db.pragma("busy_timeout = 5000");
  return db;
}

async function request(method, route, body, identity = "browser") {
  const response = await fetch(base + route, {
    method,
    headers: { "content-type": "application/json", "x-test-identity": identity },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = await response.json();
  return { status: response.status, body: payload, revisionHeader: response.headers.get("x-config-revision") };
}

function write(method, route, body, options = {}) {
  const value = {
    ...body, expected_revision: options.revision === undefined ? revision : options.revision,
    client_ref: options.ref || "test-client-" + String(++counter).padStart(8, "0"),
  };
  return request(method, route, value, options.identity || "browser").then((result) => {
    if (result.status === 200 && !result.body.replay) {
      revision = result.body.revision;
    }
    return result;
  });
}

test.before(async () => {
  process.env.TRADER_TRADING_DB_PATH = file;
  delete process.env.WATCHER_TRADING_DB;
  delete process.env.TRADING_DB_PATH;
  delete require.cache[tradingApiModule];
  const { registerTradingApi } = require("../lib/trading-api");
  const app = express();
  app.use(express.json());
  app.use((req, res, next) => {
    const requestedIdentity = req.get("x-test-identity");
    const identity = requestedIdentity === "gateway-viewer" ? "gateway" : requestedIdentity;
    if (identity !== "none") {
      req.watcherAuth = {
        identity, role: identity === "gateway" ? (requestedIdentity === "gateway-viewer" ? "viewer" : "risk_admin") : null,
        actor: identity === "gateway" ? (requestedIdentity === "gateway-viewer" ? "app:viewer" : "app:risk_admin") : "browser",
        tokenFingerprint: identity === "gateway" ? "123456789abc" : null,
      };
    }
    next();
  });
  registerTradingApi(app, { getDb: database, getStatus: () => ({}) });
  server = await new Promise((resolve) => {
    const listening = app.listen(0, "127.0.0.1", () => resolve(listening));
  });
  base = "http://127.0.0.1:" + server.address().port;
});

test.after(async () => {
  await new Promise((resolve) => server.close(resolve));
  delete require.cache[tradingApiModule];
  delete process.env.TRADER_TRADING_DB_PATH;
  fs.rmSync(temp, { recursive: true, force: true });
});

test("T0-2 empty snapshot is valid, stable, and snapshot identity only", async () => {
  const missing = await request("GET", "/api/trading/config-snapshot", undefined, "none");
  assert.equal(missing.status, 401);
  const browser = await request("GET", "/api/trading/config-snapshot");
  assert.equal(browser.status, 403);
  const first = await request("GET", "/api/trading/config-snapshot", undefined, "snapshot");
  assert.equal(first.status, 200);
  assert.equal(first.body.schema_version, "watcher-config-snapshot.v1");
  assert.equal(first.body.revision, 0);
  assert.deepEqual(first.body.accounts, []);
  assert.deepEqual(first.body.channels, []);
  assert.deepEqual(first.body.risks, []);
  const second = await request("GET", "/api/trading/config-snapshot", undefined, "snapshot");
  assert.equal(first.body.content_sha256, second.body.content_sha256);
  assert.equal(first.body.revision, second.body.revision);
  const db = database();
  assert.equal(db.prepare("SELECT count(*) AS n FROM config_revision").get().n, 1);
  assert.equal(db.prepare("SELECT count(*) AS n FROM config_audit").get().n, 0);
  db.close();
});

test("T0-2 three populated tables have whitelist, stable hash and revision headers", async () => {
  const create = await write("POST", "/api/trading/accounts", {
    account_id: "main-a", api_key: "fake-api-key", api_secret: "fake-api-secret",
    default_risk_ratio: 0.01, risk_capital_addon: 0,
  });
  assert.equal(create.status, 200);
  assert.equal(create.body.revision, 1);
  assert.equal(create.body.replay, false);
  const route = await write("POST", "/api/trading/channels", {
    channel_id: "channel-a", target_account_id: "main-a", channel_name: "secret-looking-channel",
  });
  assert.equal(route.status, 200);
  const risk = await write("POST", "/api/trading/risks", { symbol: "BTCUSDT", risk_ratio: 0.02 });
  assert.equal(risk.status, 200);
  const snapshot = await request("GET", "/api/trading/config-snapshot", undefined, "snapshot");
  assert.equal(snapshot.status, 200);
  assert.deepEqual(snapshot.body.accounts, [{
    account_id: "main-a", kind: "main", parent_account_id: null,
    execution_account_id: "main-a", enabled: true,
    risk_capital_addon: "0", default_risk: "0.01",
  }]);
  assert.deepEqual(snapshot.body.channels, [{ channel_id: "channel-a", target_account_id: "main-a" }]);
  assert.deepEqual(snapshot.body.risks, [{ symbol: "BTCUSDT", risk_ratio: "0.02" }]);
  assert.equal(snapshot.body.revision, 3);
  assert.match(snapshot.body.content_sha256, /^[0-9a-f]{64}$/);
  assert.match(snapshot.body.generated_at, /^\d{4}-\d\d-\d\dT.*\.\d{3}Z$/);
  assert.equal(JSON.stringify(snapshot.body).includes("fake-api"), false);
  assert.equal(JSON.stringify(snapshot.body).includes("secret-looking-channel"), false);
  const accounts = await request("GET", "/api/trading/accounts", undefined, "gateway");
  assert.equal(accounts.revisionHeader, "3");
  assert.equal(Object.hasOwn(accounts.body[0], "api_key"), false);
  assert.equal(Object.hasOwn(accounts.body[0], "api_secret"), false);
  const channels = await request("GET", "/api/trading/channels");
  const risks = await request("GET", "/api/trading/risks");
  assert.equal(channels.revisionHeader, "3");
  assert.equal(risks.revisionHeader, "3");
  const db = database();
  const audit = db.prepare("SELECT * FROM config_audit ORDER BY id").all();
  db.close();
  assert.equal(audit.length, 3);
  assert.ok(audit.every((row) => row.actor === "browser" && row.source === "browser"));
  assert.equal(JSON.stringify(audit).includes("fake-api"), false);
  assert.equal(JSON.stringify(audit).includes("secret-looking-channel"), false);
  assert.equal(audit[0].token_fingerprint, null);
});

test("T0-2 same-second changes, replacement and deletion change revision/hash", async () => {
  const before = (await request("GET", "/api/trading/config-snapshot", undefined, "snapshot")).body;
  const changed = await write("POST", "/api/trading/risks", { symbol: "BTCUSDT", risk_ratio: 0.03 });
  assert.equal(changed.status, 200);
  const middle = (await request("GET", "/api/trading/config-snapshot", undefined, "snapshot")).body;
  assert.equal(middle.revision, before.revision + 1);
  assert.notEqual(middle.content_sha256, before.content_sha256);
  const deleted = await write("DELETE", "/api/trading/risks/BTCUSDT", {});
  assert.equal(deleted.status, 200);
  const inserted = await write("POST", "/api/trading/risks", { symbol: "ETHUSDT", risk_ratio: 0.03 });
  assert.equal(inserted.status, 200);
  const after = (await request("GET", "/api/trading/config-snapshot", undefined, "snapshot")).body;
  assert.equal(after.risks.length, middle.risks.length);
  assert.notEqual(after.content_sha256, middle.content_sha256);
  assert.equal(after.revision, middle.revision + 2);
});

test("T0-2 revision conflict, replay and different digest preserve one write", async () => {
  const ref = "same-ref-12345678";
  const body = { symbol: "SOLUSDT", risk_ratio: 0.01 };
  const first = await write("POST", "/api/trading/risks", body, { ref });
  assert.equal(first.status, 200);
  const replay = await write("POST", "/api/trading/risks", body, { ref, revision: first.body.revision - 1 });
  assert.equal(replay.status, 200);
  assert.equal(replay.body.replay, true);
  assert.equal(replay.body.revision, first.body.revision);
  const different = await write("POST", "/api/trading/risks", { symbol: "SOLUSDT", risk_ratio: 0.02 }, { ref, revision: first.body.revision - 1 });
  assert.equal(different.status, 409);
  assert.equal(different.body.code, "idempotency_conflict");
  const stale = await write("POST", "/api/trading/risks", { symbol: "SOLUSDT", risk_ratio: 0.02 }, { revision: first.body.revision - 1 });
  assert.equal(stale.status, 409);
  assert.equal(stale.body.code, "revision_conflict");
  assert.equal(stale.body.details.current.risk_ratio, 0.01);
  assert.equal(stale.body.details.current_revision, first.body.revision);
  const db = database();
  assert.equal(db.prepare("SELECT risk_ratio FROM symbol_risk_configs WHERE symbol='SOLUSDT'").get().risk_ratio, 0.01);
  db.close();
});

test("T0-2 gateway secret input is rejected and cannot read secrets", async () => {
  const db = database();
  const before = db.prepare("SELECT revision FROM config_revision").get().revision;
  db.close();
  const rejected = await write("PUT", "/api/trading/accounts/main-a",
    { api_key: "fake-new-key" }, { identity: "gateway" });
  assert.equal(rejected.status, 400);
  assert.equal(rejected.body.code, "secret_field_rejected");
  const nested = await write("POST", "/api/trading/risks",
    { symbol: "X", risk_ratio: 0.01, extra: { sessionString: "fake" } },
    { identity: "gateway" });
  assert.equal(nested.status, 400);
  assert.equal(nested.body.code, "secret_field_rejected");
  const forbidden = await write("PUT", "/api/trading/accounts/main-a",
    { is_testnet: true }, { identity: "gateway" });
  assert.equal(forbidden.status, 400);
  assert.equal(forbidden.body.code, "invalid_body");
  const updated = await write("PUT", "/api/trading/accounts/main-a",
    { is_enabled: false }, { identity: "gateway" });
  assert.equal(updated.status, 200);
  const dbAfter = database();
  assert.equal(dbAfter.prepare("SELECT api_key FROM account_configs WHERE account_id='main-a'").get().api_key, "fake-api-key");
  assert.equal(dbAfter.prepare("SELECT revision FROM config_revision").get().revision, before + 1);
  const audit = dbAfter.prepare("SELECT * FROM config_audit ORDER BY id DESC LIMIT 1").get();
  dbAfter.close();
  assert.equal(audit.token_fingerprint, "123456789abc");
  assert.equal(audit.actor, "app:risk_admin");
  assert.equal(JSON.stringify(audit).includes("fake-new-key"), false);
});

test("T0-2 gateway write requires risk_admin in watcherAuth", async () => {
  const before = revision;
  const denied = await write("POST", "/api/trading/risks",
    { symbol: "DENIED", risk_ratio: 0.01 }, { identity: "gateway-viewer" });
  assert.equal(denied.status, 403);
  assert.equal(denied.body.code, "insufficient_scope");
  assert.equal(revision, before);
});

test("T0-2 browser keeps credential rotation while mask placeholders are rejected", async () => {
  const accounts = await request("GET", "/api/trading/accounts");
  const account = accounts.body.find((item) => item.account_id === "main-a");
  const rejected = await write("PUT", "/api/trading/accounts/main-a", {
    api_key: account.api_key,
  });
  assert.equal(rejected.status, 400);
  assert.equal(rejected.body.code, "masked_value_rejected");
  assert.equal(rejected.body.error, rejected.body.message);
  const rotated = await write("PUT", "/api/trading/accounts/main-a", {
    api_key: "fake-rotated-key", api_secret: "",
  });
  assert.equal(rotated.status, 200);
  const db = database();
  const row = db.prepare("SELECT api_key, api_secret FROM account_configs WHERE account_id='main-a'").get();
  db.close();
  assert.deepEqual(row, { api_key: "fake-rotated-key", api_secret: "fake-api-secret" });
  const gateway = await request("GET", "/api/trading/accounts", undefined, "gateway");
  assert.equal(JSON.stringify(gateway.body).includes("fake-rotated-key"), false);
});

test("T0-2 DELETE replay returns original result without another revision bump", async () => {
  const created = await write("POST", "/api/trading/risks", { symbol: "DELETE", risk_ratio: 0.01 });
  assert.equal(created.status, 200);
  const ref = "delete-ref-123456";
  const originalRevision = revision;
  const removed = await write("DELETE", "/api/trading/risks/DELETE", {}, { ref });
  assert.equal(removed.status, 200);
  const repeated = await write("DELETE", "/api/trading/risks/DELETE", {}, { ref, revision: originalRevision });
  assert.equal(repeated.status, 200);
  assert.equal(repeated.body.replay, true);
  assert.equal(repeated.body.revision, removed.body.revision);
});

test("T0-2 partial update ignores stored out-of-range risk and enforces new bounds", async () => {
  const db = database();
  db.prepare("UPDATE account_configs SET default_risk_ratio = 0.2 WHERE account_id='main-a'").run();
  db.close();
  const toggle = await write("PUT", "/api/trading/accounts/main-a", { is_enabled: true });
  assert.equal(toggle.status, 200);
  for (const bad of [0, 0.10001, -0.01, "NaN"]) {
    const result = await write("PUT", "/api/trading/accounts/main-a", { default_risk_ratio: bad });
    assert.equal(result.status, 400);
    const risk = await write("POST", "/api/trading/risks", { symbol: "LIMIT", risk_ratio: bad });
    assert.equal(risk.status, 400);
  }
  const legal = await write("PUT", "/api/trading/accounts/main-a", { default_risk_ratio: 0.1 });
  assert.equal(legal.status, 200);
  const legalRisk = await write("POST", "/api/trading/risks", { symbol: "LIMIT", risk_ratio: 0.1 });
  assert.equal(legalRisk.status, 200);
  const zeroAddon = await write("PUT", "/api/trading/accounts/main-a", { risk_capital_addon: 0 });
  assert.equal(zeroAddon.status, 200);
  for (const bad of [-1, "", false, "NaN"]) {
    const result = await write("PUT", "/api/trading/accounts/main-a", { risk_capital_addon: bad });
    assert.equal(result.status, 400);
  }
});

async function createHierarchyPair(suffix) {
  const main = "r7-main-" + suffix;
  const child = "r7-child-" + suffix;
  const parent = await write("POST", "/api/trading/accounts", {
    account_id: main, api_key: "fake-key-" + suffix, api_secret: "fake-secret-" + suffix,
    risk_capital_addon: 0, is_testnet: false,
  });
  assert.equal(parent.status, 200);
  const subaccount = await write("POST", "/api/trading/accounts", {
    account_id: child, api_key: "fake-child-key-" + suffix, api_secret: "fake-child-secret-" + suffix,
    account_type: "subaccount", parent_account_id: main,
    risk_capital_addon: 0, is_testnet: false,
  });
  assert.equal(subaccount.status, 200);
  return { main, child };
}

test("R7 gateway may disable a child after its main account is disabled", async () => {
  const { main, child } = await createHierarchyPair("disable");
  assert.equal((await write("PUT", "/api/trading/accounts/" + main,
    { is_enabled: false }, { identity: "gateway" })).status, 200);
  const result = await write("PUT", "/api/trading/accounts/" + child,
    { is_enabled: false }, { identity: "gateway" });
  assert.equal(result.status, 200);
  const db = database();
  assert.equal(db.prepare("SELECT is_enabled FROM account_configs WHERE account_id = ?").get(child).is_enabled, 0);
  db.close();
});

test("R7 gateway may change child addon with a disabled main account", async () => {
  const { main, child } = await createHierarchyPair("addon");
  assert.equal((await write("PUT", "/api/trading/accounts/" + main,
    { is_enabled: false }, { identity: "gateway" })).status, 200);
  const result = await write("PUT", "/api/trading/accounts/" + child,
    { risk_capital_addon: 5 }, { identity: "gateway" });
  assert.equal(result.status, 200);
  const db = database();
  assert.equal(db.prepare("SELECT risk_capital_addon FROM account_configs WHERE account_id = ?").get(child).risk_capital_addon, 5);
  db.close();
  const structural = await write("PUT", "/api/trading/accounts/" + child,
    { account_type: "subaccount" }, { identity: "gateway" });
  assert.equal(structural.status, 400);
  assert.equal(structural.body.code, "invalid_body");
});

test("R7 gateway enable toggle ignores stored parent environment mismatch", async () => {
  const { main, child } = await createHierarchyPair("environment");
  const db = database();
  db.prepare("UPDATE account_configs SET is_testnet = 1 WHERE account_id = ?").run(main);
  db.close();
  const toggle = await write("PUT", "/api/trading/accounts/" + child,
    { is_enabled: false }, { identity: "gateway" });
  assert.equal(toggle.status, 200);
  const structural = await write("PUT", "/api/trading/accounts/" + child,
    { parent_account_id: main }, { identity: "gateway" });
  assert.equal(structural.status, 400);
  assert.equal(structural.body.code, "invalid_body");
});

test("T0-2 snapshot order and content hash ignore account insertion order", async () => {
  const ids = ["账户α", "a-lower", "S", "M"];
  const insert = (db, id, index) => db.prepare(
    "INSERT INTO account_configs (account_id, api_key, api_secret, execution_account_id) VALUES (?, ?, ?, ?)"
  ).run(id, "fake-key", "fake-secret", "ordered-execution-" + index);
  const firstDb = database();
  ids.forEach((id, index) => insert(firstDb, id, index));
  firstDb.close();
  const first = (await request("GET", "/api/trading/config-snapshot", undefined, "snapshot")).body;
  assert.deepEqual(first.accounts.filter((row) => ids.includes(row.account_id)).map((row) => row.account_id),
    ["M", "S", "a-lower", "账户α"]);
  const secondDb = database();
  ids.forEach((id) => secondDb.prepare("DELETE FROM account_configs WHERE account_id = ?").run(id));
  ids.slice().reverse().forEach((id) => insert(secondDb, id, ids.indexOf(id)));
  secondDb.close();
  const second = (await request("GET", "/api/trading/config-snapshot", undefined, "snapshot")).body;
  assert.deepEqual(first.accounts, second.accounts);
  assert.equal(first.content_sha256, second.content_sha256);
  const cleanup = database();
  ids.forEach((id) => cleanup.prepare("DELETE FROM account_configs WHERE account_id = ?").run(id));
  cleanup.close();
});

test("T0-2 cross-process committed audit is observed after lock wait as replay", async () => {
  const { __test } = require("../lib/config-store");
  const body = { symbol: "XPROC", risk_ratio: 0.01, expected_revision: revision };
  const requestHash = __test.digest({ method: "POST", path: "/api/trading/risks", body });
  const clientRef = "xproc-ref-0001";
  const saved = JSON.stringify({ ok: true, symbol: "XPROC", risk_ratio: 0.01,
    revision: revision + 1, replay: false });
  const script = `const Database = require(${JSON.stringify(require.resolve("better-sqlite3"))});
const db = new Database(${JSON.stringify(file)});
db.exec("BEGIN IMMEDIATE");
db.prepare("INSERT INTO symbol_risk_configs (symbol, risk_ratio) VALUES ('XPROC', 0.01)").run();
db.prepare("UPDATE config_revision SET revision = revision + 1 WHERE id = 1").run();
db.prepare("INSERT INTO config_audit (idempotency_key, actor, source, token_fingerprint, operation, request_sha256, status_code, response_json, revision_before, revision_after, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)").run(
  ${JSON.stringify("browser|risk.upsert|" + clientRef)}, "browser", "browser", null, "risk.upsert",
  ${JSON.stringify(requestHash)}, 200, ${JSON.stringify(saved)}, ${revision}, ${revision + 1}, new Date().toISOString());
process.stdout.write("locked\\n");
Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 1000);
db.exec("COMMIT");
db.close();`;
  const child = spawn(process.execPath, ["-e", script], { stdio: ["ignore", "pipe", "pipe"] });
  const completed = new Promise((resolve) => child.once("exit", resolve));
  await new Promise((resolve) => child.stdout.once("data", resolve));
  const result = await request("POST", "/api/trading/risks", { ...body, client_ref: clientRef });
  assert.equal(await completed, 0);
  assert.equal(result.status, 200);
  assert.equal(result.body.replay, true);
  revision = result.body.revision;
});

test("T0-2 SQLite CHECK maps to 400 validation_failed", async () => {
  const db = database();
  db.pragma("ignore_check_constraints = ON");
  db.prepare("UPDATE account_configs SET risk_capital_addon = -1 WHERE account_id = 'main-a'").run();
  db.close();
  const failed = await write("PUT", "/api/trading/accounts/main-a", { is_enabled: true });
  assert.equal(failed.status, 400);
  assert.equal(failed.body.code, "validation_failed");
  const repair = database();
  repair.pragma("ignore_check_constraints = ON");
  repair.prepare("UPDATE account_configs SET risk_capital_addon = 0 WHERE account_id = 'main-a'").run();
  repair.close();
});

test("T0-2 unknown exception is logged without request secret", async () => {
  const secret = "fake-unknown-error-secret";
  const db = database();
  db.exec("CREATE TRIGGER reject_account_update BEFORE UPDATE ON account_configs " +
    "BEGIN SELECT RAISE(ABORT, 'fake-unknown-error-secret'); END");
  db.close();
  const logs = [];
  const original = console.error;
  console.error = (...parts) => logs.push(parts);
  let result;
  try {
    result = await write("PUT", "/api/trading/accounts/main-a", { api_key: secret });
  } finally {
    console.error = original;
    const cleanup = database();
    cleanup.exec("DROP TRIGGER reject_account_update");
    cleanup.close();
  }
  assert.equal(result.status, 500);
  assert.ok(logs.length > 0);
  assert.equal(JSON.stringify(logs).includes(secret), false);
});

test("T0-2 concurrent same ref commits once and failed audit rolls back", async () => {
  const body = { symbol: "CONCUR", risk_ratio: 0.01, expected_revision: revision, client_ref: "concurrent-ref-123" };
  const both = await Promise.all([
    request("POST", "/api/trading/risks", body),
    request("POST", "/api/trading/risks", body),
  ]);
  assert.deepEqual(both.map((x) => x.status), [200, 200]);
  assert.deepEqual(both.map((x) => x.body.replay).sort(), [false, true]);
  revision = both[0].body.revision;
  const db = database();
  db.exec("CREATE TRIGGER reject_config_audit BEFORE INSERT ON config_audit BEGIN SELECT RAISE(ABORT, 'synthetic crash'); END");
  const before = db.prepare("SELECT revision FROM config_revision").get().revision;
  const auditsBefore = db.prepare("SELECT count(*) AS n FROM config_audit").get().n;
  db.close();
  const failed = await write("POST", "/api/trading/risks", { symbol: "ROLLBACK", risk_ratio: 0.01 });
  assert.equal(failed.status, 500);
  const check = database();
  assert.equal(check.prepare("SELECT revision FROM config_revision").get().revision, before);
  assert.equal(check.prepare("SELECT * FROM symbol_risk_configs WHERE symbol='ROLLBACK'").get(), undefined);
  assert.equal(check.prepare("SELECT count(*) AS n FROM config_audit").get().n, auditsBefore);
  check.exec("DROP TRIGGER reject_config_audit");
  check.close();
});

test("T0-2 lock timeout is 503 db_busy without partial write", async () => {
  const lock = database();
  lock.exec("BEGIN IMMEDIATE");
  const before = revision;
  const failed = await write("POST", "/api/trading/risks", { symbol: "BUSY", risk_ratio: 0.01 });
  assert.equal(failed.status, 503);
  assert.equal(failed.body.code, "db_busy");
  assert.equal(failed.body.details.busy_timeout_ms, 5000);
  lock.exec("ROLLBACK");
  lock.close();
  const db = database();
  assert.equal(db.prepare("SELECT revision FROM config_revision").get().revision, before);
  assert.equal(db.prepare("SELECT * FROM symbol_risk_configs WHERE symbol='BUSY'").get(), undefined);
  db.close();
});

test("T0-2 invalid data fails closed and missing table is unreadable", async () => {
  const db = database();
  db.prepare("UPDATE symbol_risk_configs SET risk_ratio = -1 WHERE symbol='ETHUSDT'").run();
  db.close();
  const invalid = await request("GET", "/api/trading/config-snapshot", undefined, "snapshot");
  assert.equal(invalid.status, 500);
  assert.equal(invalid.body.code, "snapshot_invalid");
  assert.ok(invalid.body.details.violations.some((item) => item.rule === "invalid_number"));
  const repair = database();
  repair.prepare("UPDATE symbol_risk_configs SET risk_ratio = 0.02 WHERE symbol='ETHUSDT'").run();
  repair.exec("DROP TABLE symbol_risk_configs");
  repair.close();
  const unreadable = await request("GET", "/api/trading/config-snapshot", undefined, "snapshot");
  assert.equal(unreadable.status, 500);
  assert.equal(unreadable.body.code, "snapshot_unreadable");
});

test("T0-2 missing required snapshot column is invalid, not an empty table", () => {
  const db = new Database(":memory:");
  db.exec("CREATE TABLE config_revision (id INTEGER PRIMARY KEY, revision INTEGER);" +
    "INSERT INTO config_revision VALUES (1, 0);" +
    "CREATE TABLE account_configs (account_id TEXT PRIMARY KEY);" +
    "CREATE TABLE channel_routing (channel_id TEXT, target_account_id TEXT);" +
    "CREATE TABLE symbol_risk_configs (symbol TEXT, risk_ratio REAL);");
  const { __test } = require("../lib/config-store");
  assert.throws(() => __test.makeSnapshot(db), (error) => {
    assert.equal(error.code, "snapshot_invalid");
    assert.ok(error.details.violations.some((item) => item.rule === "missing_field"));
    return true;
  });
  db.close();
});
