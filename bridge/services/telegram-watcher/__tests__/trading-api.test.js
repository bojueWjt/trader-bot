const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const Database = require("better-sqlite3");
const express = require("express");

const tradingApiPath = require.resolve("../lib/trading-api");
const originalTraderTradingDbPath = process.env.TRADER_TRADING_DB_PATH;
const originalWatcherTradingDbPath = process.env.WATCHER_TRADING_DB;
const originalTradingDbPath = process.env.TRADING_DB_PATH;
const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "telegram-watcher-accounts-"));
const dbPath = path.join(tempDir, "trading.db");

let baseUrl = "";
let server = false;

function createLegacyDatabase() {
  const db = new Database(dbPath);
  db.pragma("foreign_keys = ON");
  db.exec(`
    CREATE TABLE account_configs (
      account_id          TEXT PRIMARY KEY,
      api_key             TEXT NOT NULL,
      api_secret          TEXT NOT NULL,
      default_risk_ratio  REAL DEFAULT 0.01,
      is_testnet          INTEGER DEFAULT 1
    );

    CREATE TABLE channel_routing (
      channel_id          TEXT PRIMARY KEY,
      target_account_id   TEXT NOT NULL,
      FOREIGN KEY (target_account_id) REFERENCES account_configs(account_id)
    );
  `);
  db.prepare(`
    INSERT INTO account_configs (
      account_id,
      api_key,
      api_secret,
      default_risk_ratio,
      is_testnet
    )
    VALUES (?, ?, ?, ?, ?)
  `).run("legacy-main", "legacy-key", "legacy-secret", 0.02, 0);
  db.prepare(`
    INSERT INTO channel_routing (channel_id, target_account_id)
    VALUES (?, ?)
  `).run("legacy-channel", "legacy-main");
  db.close();
}

async function request(method, pathname, body) {
  const options = { method };
  if (body !== undefined) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  const response = await fetch(baseUrl + pathname, options);
  return {
    status: response.status,
    body: await response.json(),
  };
}

test.before(async () => {
  createLegacyDatabase();
  delete process.env.TRADER_TRADING_DB_PATH;
  delete process.env.WATCHER_TRADING_DB;
  process.env.TRADING_DB_PATH = dbPath;
  delete require.cache[tradingApiPath];

  const { registerTradingApi } = require("../lib/trading-api");
  const app = express();
  app.use(express.json());
  registerTradingApi(app, {
    getDb() {
      return new Database(dbPath);
    },
    getStatus() {
      return { running: false };
    },
  });

  server = await new Promise((resolve) => {
    const listeningServer = app.listen(0, "127.0.0.1", () => {
      resolve(listeningServer);
    });
  });
  const address = server.address();
  baseUrl = `http://127.0.0.1:${address.port}`;
});

test.after(async () => {
  if (server) {
    await new Promise((resolve, reject) => {
      server.close((err) => {
        if (err) {
          reject(err);
          return;
        }
        resolve();
      });
    });
  }
  delete require.cache[tradingApiPath];
  if (originalTraderTradingDbPath === undefined) {
    delete process.env.TRADER_TRADING_DB_PATH;
  } else {
    process.env.TRADER_TRADING_DB_PATH = originalTraderTradingDbPath;
  }
  if (originalWatcherTradingDbPath === undefined) {
    delete process.env.WATCHER_TRADING_DB;
  } else {
    process.env.WATCHER_TRADING_DB = originalWatcherTradingDbPath;
  }
  if (originalTradingDbPath === undefined) {
    delete process.env.TRADING_DB_PATH;
  } else {
    process.env.TRADING_DB_PATH = originalTradingDbPath;
  }
  fs.rmSync(tempDir, { recursive: true, force: true });
});

test("resolves canonical trading DB path and accepts matching legacy aliases", () => {
  const { __test } = require("../lib/trading-api");

  assert.equal(
    __test.resolveTradingDbPath({}),
    __test.DEFAULT_TRADING_DB_PATH
  );
  assert.equal(
    __test.resolveTradingDbPath({ TRADING_DB_PATH: "/data/trading.db" }),
    "/data/trading.db"
  );
  assert.equal(
    __test.resolveTradingDbPath({
      TRADER_TRADING_DB_PATH: "/data/watcher-trading.db",
      WATCHER_TRADING_DB: "/data/watcher-trading.db",
      TRADING_DB_PATH: "/data/watcher-trading.db",
    }),
    "/data/watcher-trading.db"
  );
});

test("rejects conflicting trading DB path aliases before opening SQLite", () => {
  const { __test } = require("../lib/trading-api");

  assert.throws(
    () => __test.resolveTradingDbPath({
      TRADER_TRADING_DB_PATH: "/data/a.db",
      TRADING_DB_PATH: "/data/b.db",
    }),
    /conflicting trading DB path/
  );
});

test("migrates legacy accounts and preserves channel routing", async () => {
  const accounts = await request("GET", "/api/trading/accounts");

  assert.equal(accounts.status, 200);
  assert.equal(accounts.body.length, 1);
  assert.deepEqual(accounts.body[0], {
    account_id: "legacy-main",
    api_key: "lega...-key",
    api_secret: "lega...cret",
    default_risk_ratio: 0.02,
    is_testnet: 0,
    account_type: "main",
    parent_account_id: "",
    execution_account_id: "legacy-main",
    risk_capital_multiplier: null,
    is_enabled: 0,
    channel_count: 1,
    subaccount_count: 0,
  });

  const channels = await request("GET", "/api/trading/channels");
  assert.equal(channels.status, 200);
  assert.deepEqual(channels.body, [
    {
      channel_id: "legacy-channel",
      target_account_id: "legacy-main",
      channel_name: "",
      target_account_type: "main",
      parent_account_id: "",
      execution_account_id: "legacy-main",
      target_is_enabled: 0,
    },
  ]);
});

test("requires explicit configuration before a migrated account is tradable", async () => {
  const disabledRoute = await request("POST", "/api/trading/channels", {
    channel_id: "disabled-legacy-channel",
    target_account_id: "legacy-main",
  });
  assert.equal(disabledRoute.status, 400);
  assert.equal(disabledRoute.body.error, "Target account is disabled");

  const configuredOnly = await request("PUT", "/api/trading/accounts/legacy-main", {
    risk_capital_multiplier: 1,
  });
  assert.equal(configuredOnly.status, 200);

  const accounts = await request("GET", "/api/trading/accounts");
  const legacyAccount = accounts.body.find((account) => {
    return account.account_id === "legacy-main";
  });
  assert.equal(legacyAccount.risk_capital_multiplier, 1);
  assert.equal(legacyAccount.is_enabled, 0);

  const enabledWithoutMultiplier = await request(
    "PUT",
    "/api/trading/accounts/legacy-main",
    { is_enabled: true }
  );
  assert.equal(enabledWithoutMultiplier.status, 400);
  assert.equal(
    enabledWithoutMultiplier.body.error,
    "enabling an account requires an explicit risk_capital_multiplier"
  );

  const enabled = await request("PUT", "/api/trading/accounts/legacy-main", {
    risk_capital_multiplier: 1,
    is_enabled: true,
  });
  assert.equal(enabled.status, 200);

  const enabledAccounts = await request("GET", "/api/trading/accounts");
  const enabledLegacyAccount = enabledAccounts.body.find((account) => {
    return account.account_id === "legacy-main";
  });
  assert.equal(enabledLegacyAccount.risk_capital_multiplier, 1);
  assert.equal(enabledLegacyAccount.is_enabled, 1);
});

test("preserves legal multipliers and disables invalid stored values", () => {
  const migrationDbPath = path.join(tempDir, "multiplier-migration.db");
  const db = new Database(migrationDbPath);
  db.exec(`
    CREATE TABLE account_configs (
      account_id TEXT PRIMARY KEY,
      api_key TEXT NOT NULL,
      api_secret TEXT NOT NULL,
      default_risk_ratio REAL DEFAULT 0.01,
      is_testnet INTEGER DEFAULT 1,
      account_type TEXT NOT NULL DEFAULT 'main',
      parent_account_id TEXT NOT NULL DEFAULT '',
      execution_account_id TEXT NOT NULL DEFAULT '',
      risk_capital_multiplier REAL,
      is_enabled INTEGER NOT NULL DEFAULT 1
    );
  `);
  const insertAccount = db.prepare(`
    INSERT INTO account_configs (
      account_id,
      api_key,
      api_secret,
      execution_account_id,
      risk_capital_multiplier,
      is_enabled
    )
    VALUES (?, 'key', 'secret', ?, ?, ?)
  `);
  insertAccount.run("legal-one", "execution-legal", 1, 1);
  insertAccount.run("disabled-one", "execution-disabled", 1, 0);
  insertAccount.run("invalid-zero", "execution-zero", 0, 1);
  insertAccount.run("invalid-null", "execution-null", null, 1);

  require("../lib/trading-api").__test.migrateAccountSchema(db);

  const rows = db.prepare(`
    SELECT account_id, risk_capital_multiplier, is_enabled
    FROM account_configs
    ORDER BY account_id
  `).all();
  db.close();

  assert.deepEqual(rows, [
    {
      account_id: "disabled-one",
      risk_capital_multiplier: 1,
      is_enabled: 0,
    },
    {
      account_id: "invalid-null",
      risk_capital_multiplier: null,
      is_enabled: 0,
    },
    {
      account_id: "invalid-zero",
      risk_capital_multiplier: 0,
      is_enabled: 0,
    },
    {
      account_id: "legal-one",
      risk_capital_multiplier: 1,
      is_enabled: 1,
    },
  ]);
});

test("creates tradable main and subaccounts with independent credentials", async () => {
  const main = await request("POST", "/api/trading/accounts", {
    account_id: "main-live",
    api_key: "main-live-key",
    api_secret: "main-live-secret",
    default_risk_ratio: 0.01,
    is_testnet: false,
    execution_account_id: "account-main",
    risk_capital_multiplier: 1,
    is_enabled: true,
  });
  assert.equal(main.status, 200);
  assert.equal(main.body.account_id, "main-live");

  const subaccount = await request("POST", "/api/trading/accounts", {
    account_id: "channel-sub",
    api_key: "channel-sub-key",
    api_secret: "channel-sub-secret",
    default_risk_ratio: 0.02,
    is_testnet: false,
    account_type: "subaccount",
    parent_account_id: "main-live",
    execution_account_id: "account-sub",
    risk_capital_multiplier: 2,
    is_enabled: true,
  });
  assert.equal(subaccount.status, 200);
  assert.equal(subaccount.body.account_id, "channel-sub");

  const db = new Database(dbPath, { readonly: true });
  const rows = db.prepare(`
    SELECT
      account_id,
      api_key,
      api_secret,
      account_type,
      parent_account_id,
      execution_account_id,
      risk_capital_multiplier
    FROM account_configs
    WHERE account_id IN ('main-live', 'channel-sub')
    ORDER BY account_id
  `).all();
  db.close();
  assert.deepEqual(rows, [
    {
      account_id: "channel-sub",
      api_key: "channel-sub-key",
      api_secret: "channel-sub-secret",
      account_type: "subaccount",
      parent_account_id: "main-live",
      execution_account_id: "account-sub",
      risk_capital_multiplier: 2,
    },
    {
      account_id: "main-live",
      api_key: "main-live-key",
      api_secret: "main-live-secret",
      account_type: "main",
      parent_account_id: "",
      execution_account_id: "account-main",
      risk_capital_multiplier: 1,
    },
  ]);

  const duplicate = await request("POST", "/api/trading/accounts", {
    account_id: "main-live",
    api_key: "duplicate-key",
    api_secret: "duplicate-secret",
    risk_capital_multiplier: 1,
  });
  assert.equal(duplicate.status, 409);
});

test("accepts existing unicode credential aliases with fixed execution ids", async () => {
  const main = await request("POST", "/api/trading/accounts", {
    account_id: "jiataotx@gmail.com",
    api_key: "main-unicode-key",
    api_secret: "main-unicode-secret",
    is_testnet: false,
    execution_account_id: "account-a",
    risk_capital_multiplier: 1,
  });
  assert.equal(main.status, 200);

  const subaccount = await request("POST", "/api/trading/accounts", {
    account_id: "泰山",
    api_key: "sub-unicode-key",
    api_secret: "sub-unicode-secret",
    is_testnet: false,
    account_type: "subaccount",
    parent_account_id: "jiataotx@gmail.com",
    execution_account_id: "account-c",
    risk_capital_multiplier: 2,
  });
  assert.equal(subaccount.status, 200);

  const accounts = await request("GET", "/api/trading/accounts");
  assert.equal(accounts.status, 200);
  const byId = new Map(accounts.body.map((account) => {
    return [account.account_id, account];
  }));
  assert.equal(byId.get("泰山").parent_account_id, "jiataotx@gmail.com");
  assert.equal(byId.get("泰山").execution_account_id, "account-c");
});

test("validates subaccount parent and environment", async () => {
  const missingMultiplier = await request("POST", "/api/trading/accounts", {
    account_id: "missing-multiplier",
    api_key: "key",
    api_secret: "secret",
  });
  assert.equal(missingMultiplier.status, 400);
  assert.equal(
    missingMultiplier.body.error,
    "risk_capital_multiplier is required and must be greater than 0"
  );

  const invalidMultiplier = await request("POST", "/api/trading/accounts", {
    account_id: "invalid-multiplier",
    api_key: "key",
    api_secret: "secret",
    risk_capital_multiplier: 0,
  });
  assert.equal(invalidMultiplier.status, 400);

  const invalidEnvironment = await request("POST", "/api/trading/accounts", {
    account_id: "invalid-environment",
    api_key: "key",
    api_secret: "secret",
    is_testnet: "yes",
    risk_capital_multiplier: 1,
  });
  assert.equal(invalidEnvironment.status, 400);

  const missingParent = await request("POST", "/api/trading/accounts", {
    account_id: "missing-parent",
    api_key: "key",
    api_secret: "secret",
    account_type: "subaccount",
    is_testnet: false,
    risk_capital_multiplier: 1,
  });
  assert.equal(missingParent.status, 400);

  const unknownParent = await request("POST", "/api/trading/accounts", {
    account_id: "unknown-parent",
    api_key: "key",
    api_secret: "secret",
    account_type: "subaccount",
    parent_account_id: "unknown",
    is_testnet: false,
    risk_capital_multiplier: 1,
  });
  assert.equal(unknownParent.status, 400);

  const nestedParent = await request("POST", "/api/trading/accounts", {
    account_id: "nested-parent",
    api_key: "key",
    api_secret: "secret",
    account_type: "subaccount",
    parent_account_id: "channel-sub",
    is_testnet: false,
    risk_capital_multiplier: 1,
  });
  assert.equal(nestedParent.status, 400);

  const environmentMismatch = await request("POST", "/api/trading/accounts", {
    account_id: "testnet-child",
    api_key: "key",
    api_secret: "secret",
    account_type: "subaccount",
    parent_account_id: "main-live",
    is_testnet: true,
    risk_capital_multiplier: 1,
  });
  assert.equal(environmentMismatch.status, 400);
});

test("routes channels to both main and subaccounts", async () => {
  const mainRoute = await request("POST", "/api/trading/channels", {
    channel_id: "main-channel",
    target_account_id: "main-live",
    channel_name: "Main Signals",
  });
  assert.equal(mainRoute.status, 200);

  const subRoute = await request("POST", "/api/trading/channels", {
    channel_id: "sub-channel",
    target_account_id: "channel-sub",
    channel_name: "Sub Signals",
  });
  assert.equal(subRoute.status, 200);

  const channels = await request("GET", "/api/trading/channels");
  const byId = new Map();
  for (const channel of channels.body) {
    byId.set(channel.channel_id, channel);
  }
  assert.equal(byId.get("main-channel").target_account_type, "main");
  assert.equal(byId.get("main-channel").parent_account_id, "");
  assert.equal(byId.get("main-channel").execution_account_id, "account-main");
  assert.equal(byId.get("sub-channel").target_account_type, "subaccount");
  assert.equal(byId.get("sub-channel").parent_account_id, "main-live");
  assert.equal(byId.get("sub-channel").execution_account_id, "account-sub");

  const unknownTarget = await request("POST", "/api/trading/channels", {
    channel_id: "unknown-channel",
    target_account_id: "missing",
  });
  assert.equal(unknownTarget.status, 400);
});

test("updates account settings while blank credentials retain current values", async () => {
  const invalidEnvironment = await request("PUT", "/api/trading/accounts/channel-sub", {
    is_testnet: "live",
  });
  assert.equal(invalidEnvironment.status, 400);

  const updated = await request("PUT", "/api/trading/accounts/channel-sub", {
    api_key: "",
    api_secret: "",
    default_risk_ratio: 0.035,
    risk_capital_multiplier: 2.5,
    execution_account_id: "account-sub-v2",
    is_testnet: false,
    account_type: "subaccount",
    parent_account_id: "main-live",
  });
  assert.equal(updated.status, 200);

  const db = new Database(dbPath, { readonly: true });
  const account = db.prepare(`
    SELECT
      api_key,
      api_secret,
      default_risk_ratio,
      account_type,
      parent_account_id,
      execution_account_id,
      risk_capital_multiplier
    FROM account_configs
    WHERE account_id = 'channel-sub'
  `).get();
  db.close();
  assert.deepEqual(account, {
    api_key: "channel-sub-key",
    api_secret: "channel-sub-secret",
    default_risk_ratio: 0.035,
    account_type: "subaccount",
    parent_account_id: "main-live",
    execution_account_id: "account-sub-v2",
    risk_capital_multiplier: 2.5,
  });

  const protectedMain = await request("PUT", "/api/trading/accounts/main-live", {
    account_type: "subaccount",
    parent_account_id: "legacy-main",
    is_testnet: false,
  });
  assert.equal(protectedMain.status, 409);
});

test("retains the configured multiplier when unrelated settings change", async () => {
  const updated = await request("PUT", "/api/trading/accounts/channel-sub", {
    api_key: "",
    api_secret: "",
    default_risk_ratio: 0.04,
  });
  assert.equal(updated.status, 200);

  const db = new Database(dbPath, { readonly: true });
  const account = db.prepare(`
    SELECT default_risk_ratio, risk_capital_multiplier
    FROM account_configs
    WHERE account_id = 'channel-sub'
  `).get();
  db.close();

  assert.deepEqual(account, {
    default_risk_ratio: 0.04,
    risk_capital_multiplier: 2.5,
  });
});

test("protects accounts referenced by children, routes, or active orders", async () => {
  const mainWithChild = await request("DELETE", "/api/trading/accounts/main-live");
  assert.equal(mainWithChild.status, 409);
  assert.equal(mainWithChild.body.subaccount_count, 1);

  const routedSubaccount = await request("DELETE", "/api/trading/accounts/channel-sub");
  assert.equal(routedSubaccount.status, 409);
  assert.equal(routedSubaccount.body.channel_count, 1);

  const removedRoute = await request("DELETE", "/api/trading/channels/sub-channel");
  assert.equal(removedRoute.status, 200);

  const db = new Database(dbPath);
  db.prepare(`
    INSERT INTO active_orders (account_id, symbol, side, status)
    VALUES (?, ?, ?, ?)
  `).run("channel-sub", "BTCUSDT", "BUY", "OPEN");
  db.close();

  const activeSubaccount = await request("DELETE", "/api/trading/accounts/channel-sub");
  assert.equal(activeSubaccount.status, 409);
  assert.equal(activeSubaccount.body.active_order_count, 1);
});
