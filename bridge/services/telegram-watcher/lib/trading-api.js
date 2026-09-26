const Database = require("better-sqlite3");
const {
  maskSecret,
  safeErrorMessage,
  sanitizeForResponse,
} = require("./safe-log");

const DEFAULT_TRADING_DB_PATH = "/var/lib/docker/volumes/trader_signal-data/_data/watcher-trading.db";
const CANONICAL_TRADING_DB_ENV = "TRADER_TRADING_DB_PATH";
const LEGACY_TRADING_DB_ENVS = ["WATCHER_TRADING_DB", "TRADING_DB_PATH"];
const TRADING_DB_PATH = resolveTradingDbPath(process.env);
const CREDENTIAL_ACCOUNT_ID_PATTERN = /^[^\u0000-\u001F\u007F]{1,128}$/u;
const EXECUTION_ACCOUNT_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const ACCOUNT_TYPES = new Set(["main", "subaccount"]);
const ACTIVE_ORDER_STATUSES = ["PENDING", "OPEN", "PARTIAL_CLOSED"];

function normalizeDbPath(value) {
  return String(value || "").trim();
}

function resolveTradingDbPath(env) {
  const configured = [];
  for (const name of [CANONICAL_TRADING_DB_ENV, ...LEGACY_TRADING_DB_ENVS]) {
    const value = normalizeDbPath(env[name]);
    if (value) {
      configured.push({ name, value });
    }
  }
  if (!configured.length) {
    return DEFAULT_TRADING_DB_PATH;
  }
  const canonical = configured[0].value;
  for (const item of configured.slice(1)) {
    if (item.value !== canonical) {
      throw new Error(
        "conflicting trading DB path environment: "
        + `${configured[0].name}=${canonical} `
        + `${item.name}=${item.value}`
      );
    }
  }
  return canonical;
}

function getTradingDb() {
  const db = new Database(TRADING_DB_PATH);
  db.pragma("busy_timeout = 5000");
  db.pragma("journal_mode = WAL");
  db.pragma("foreign_keys = ON");
  return db;
}

// mirrors SCHEMA_SQL in skills/crypto-trader/scripts/db_manager.py — on the Mac
// the Python side created these tables; in the container this is the only writer
const TRADING_SCHEMA_SQL = `
CREATE TABLE IF NOT EXISTS account_configs (
    account_id          TEXT PRIMARY KEY,
    api_key             TEXT NOT NULL,
    api_secret          TEXT NOT NULL,
    default_risk_ratio  REAL DEFAULT 0.01,
    is_testnet          INTEGER DEFAULT 1,
    account_type        TEXT NOT NULL DEFAULT 'main',
    parent_account_id   TEXT NOT NULL DEFAULT '',
    execution_account_id TEXT NOT NULL DEFAULT '',
    risk_capital_multiplier REAL NOT NULL DEFAULT 1,
    risk_capital_addon REAL NOT NULL DEFAULT 0,
    is_enabled          INTEGER NOT NULL DEFAULT 1,
    CHECK (account_type IN ('main', 'subaccount')),
    CHECK (
      risk_capital_multiplier IS NULL
      OR risk_capital_multiplier > 0
    ),
    CHECK (risk_capital_addon >= 0),
    CHECK (is_enabled IN (0, 1))
);

CREATE TABLE IF NOT EXISTS channel_routing (
    channel_id          TEXT PRIMARY KEY,
    target_account_id   TEXT NOT NULL,
    channel_name        TEXT DEFAULT '',
    FOREIGN KEY (target_account_id) REFERENCES account_configs(account_id)
);

CREATE TABLE IF NOT EXISTS symbol_risk_configs (
    symbol              TEXT PRIMARY KEY,
    risk_ratio          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS active_orders (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id          TEXT,
    account_id          TEXT,
    symbol              TEXT,
    side                TEXT,
    entry_price         REAL,
    stop_loss           REAL,
    take_profit         REAL,
    quantity            REAL,
    binance_order_id    TEXT,
    binance_sl_order_id TEXT,
    binance_tp_order_id TEXT,
    status              TEXT DEFAULT 'PENDING',
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS briefings (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id          TEXT,
    content             TEXT NOT NULL,
    category            TEXT DEFAULT 'analysis',
    created_at          TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS signal_operations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id           TEXT NOT NULL,
    operation_type      TEXT NOT NULL,
    symbol              TEXT,
    side                TEXT,
    detail              TEXT DEFAULT '{}',
    created_at          TEXT DEFAULT (datetime('now')),
    UNIQUE(signal_id, operation_type)
);
`;

function ensureTradingTables() {
  let db;
  try {
    db = getTradingDb();
    const versioned = Boolean(db.prepare(
      "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'config_revision'"
    ).get());
    if (versioned) {
      for (const name of ["account_configs", "channel_routing", "symbol_risk_configs"]) {
        const present = db.prepare(
          "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?"
        ).get(name);
        if (!present) {
          throw new Error("versioned trading configuration table missing: " + name);
        }
      }
    }
    db.transaction(() => {
      db.exec(TRADING_SCHEMA_SQL);
      if (!versioned) {
        migrateAccountSchema(db);
      }
      require("./config-store").ensureConfigTables(db);
    }).immediate();
  } catch (err) {
    console.log("[db] Failed to ensure trading tables:", safeErrorMessage(err));
    process.exit(1);
  } finally {
    closeDb(db);
  }
}

function migrateAccountSchema(db) {
  const columns = new Set(
    db.prepare("PRAGMA table_info(account_configs)").all().map((row) => {
      return row.name;
    })
  );
  const multiplierColumnWasMissing = !columns.has("risk_capital_multiplier");
  const addonColumnWasMissing = !columns.has("risk_capital_addon");

  if (!columns.has("account_type")) {
    db.exec("ALTER TABLE account_configs ADD COLUMN account_type TEXT NOT NULL DEFAULT 'main'");
  }
  if (!columns.has("parent_account_id")) {
    db.exec("ALTER TABLE account_configs ADD COLUMN parent_account_id TEXT NOT NULL DEFAULT ''");
  }
  if (multiplierColumnWasMissing) {
    db.exec(
      "ALTER TABLE account_configs "
      + "ADD COLUMN risk_capital_multiplier REAL NOT NULL DEFAULT 1"
    );
  }
  if (addonColumnWasMissing) {
    db.exec(
      "ALTER TABLE account_configs "
      + "ADD COLUMN risk_capital_addon REAL NOT NULL DEFAULT 0"
    );
  }
  if (!columns.has("execution_account_id")) {
    db.exec(
      "ALTER TABLE account_configs "
      + "ADD COLUMN execution_account_id TEXT NOT NULL DEFAULT ''"
    );
  }
  if (!columns.has("is_enabled")) {
    db.exec(
      "ALTER TABLE account_configs "
      + "ADD COLUMN is_enabled INTEGER NOT NULL DEFAULT 1"
    );
  }

  db.exec(`
    UPDATE account_configs
    SET account_type = 'main'
    WHERE account_type IS NULL
       OR account_type = ''
       OR account_type NOT IN ('main', 'subaccount');

    UPDATE account_configs
    SET parent_account_id = ''
    WHERE account_type = 'main'
       OR parent_account_id IS NULL;

    UPDATE account_configs
    SET execution_account_id = account_id
    WHERE execution_account_id IS NULL
       OR execution_account_id = '';

    CREATE INDEX IF NOT EXISTS idx_account_configs_parent
    ON account_configs (parent_account_id, account_type);

    CREATE UNIQUE INDEX IF NOT EXISTS idx_account_configs_execution_account
    ON account_configs (execution_account_id);
  `);

  disableAccountsWithInvalidMultiplier(db);
  disableAccountsWithInvalidAddon(db);
  migrateChannelRoutingSchema(db);
}

function migrateChannelRoutingSchema(db) {
  const columns = new Set(
    db.prepare("PRAGMA table_info(channel_routing)").all().map((row) => {
      return row.name;
    })
  );
  if (columns.size === 0) {
    return;
  }
  if (!columns.has("channel_name")) {
    db.exec(
      "ALTER TABLE channel_routing "
      + "ADD COLUMN channel_name TEXT DEFAULT ''"
    );
  }
}

function disableAccountsWithInvalidMultiplier(db) {
  const rows = db.prepare(`
    SELECT account_id, risk_capital_multiplier
    FROM account_configs
  `).all();
  const disableAccount = db.prepare(`
    UPDATE account_configs
    SET is_enabled = 0
    WHERE account_id = ?
  `);
  const disableInvalidAccounts = db.transaction((accounts) => {
    for (const account of accounts) {
      const multiplier = Number(account.risk_capital_multiplier);
      if (
        account.risk_capital_multiplier === null
        || !Number.isFinite(multiplier)
        || multiplier <= 0
      ) {
        disableAccount.run(account.account_id);
      }
    }
  });
  disableInvalidAccounts(rows);
}

function disableAccountsWithInvalidAddon(db) {
  const rows = db.prepare(`
    SELECT account_id, risk_capital_addon
    FROM account_configs
  `).all();
  const disableAccount = db.prepare(`
    UPDATE account_configs
    SET is_enabled = 0
    WHERE account_id = ?
  `);
  const disableInvalidAccounts = db.transaction((accounts) => {
    for (const account of accounts) {
      const addon = Number(account.risk_capital_addon);
      if (
        account.risk_capital_addon === null
        || account.risk_capital_addon === ""
        || !Number.isFinite(addon)
        || addon < 0
      ) {
        disableAccount.run(account.account_id);
      }
    }
  });
  disableInvalidAccounts(rows);
}

function ensureTelegramMessagesTable() {
  let db;
  try {
    db = getTradingDb();
    db.exec(`
      CREATE TABLE IF NOT EXISTS telegram_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        msg_id INTEGER,
        channel_id TEXT,
        chat_title TEXT DEFAULT '',
        sender TEXT DEFAULT '',
        text TEXT DEFAULT '',
        has_media INTEGER DEFAULT 0,
        media_type TEXT DEFAULT '',
        media_filename TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now'))
      )
    `);
    db.exec(`DELETE FROM telegram_messages WHERE created_at < datetime('now', '-7 days')`);
  } catch (err) {
    console.log("[db] Failed to ensure telegram_messages table:", safeErrorMessage(err));
  } finally {
    closeDb(db);
  }
}

function saveTelegramMessage(entry) {
  let db;
  let mediaType = "";
  let mediaFilename = "";
  if (entry.media) {
    mediaType = entry.media.type || "";
    mediaFilename = entry.media.filename || "";
  }
  try {
    db = getTradingDb();
    db.prepare(`
      INSERT INTO telegram_messages (msg_id, channel_id, chat_title, sender, text, has_media, media_type, media_filename)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    `).run(
      entry.id,
      entry.chatId || "",
      entry.chatTitle || "",
      entry.sender || "",
      entry.text || "",
      entry.media ? 1 : 0,
      mediaType,
      mediaFilename
    );
  } catch (err) {
    console.log("[db] Failed to save message:", safeErrorMessage(err));
  } finally {
    closeDb(db);
  }
}

function registerTradingApi(app, priceMonitor) {
  ensureTradingTables();
  require("./config-store").registerConfigRoutes(app, getTradingDb);
  registerOrderRoutes(app);
  registerBriefingRoutes(app);
  registerTelegramMessageRoutes(app);
  registerPriceAlertRoutes(app, priceMonitor);
}

function registerOrderRoutes(app) {
  app.get("/api/trading/orders", (req, res) => {
    let db;
    try {
      db = getTradingDb();
      let query = "SELECT * FROM active_orders WHERE 1=1";
      const params = [];
      if (req.query.status) {
        query += " AND status = ?";
        params.push(req.query.status);
      }
      if (req.query.channel) {
        query += " AND channel_id = ?";
        params.push(req.query.channel);
      }
      query += " ORDER BY created_at DESC";
      const rows = db.prepare(query).all(...params);
      res.json(sanitizeForResponse(rows));
    } catch (err) {
      sendServerError(res, err);
    } finally {
      closeDb(db);
    }
  });

  app.get("/api/trading/orders/active", (req, res) => {
    let db;
    try {
      db = getTradingDb();
      const rows = db.prepare(
        "SELECT * FROM active_orders WHERE status IN ('OPEN', 'PARTIAL_CLOSED') ORDER BY created_at DESC"
      ).all();
      res.json(sanitizeForResponse(rows));
    } catch (err) {
      sendServerError(res, err);
    } finally {
      closeDb(db);
    }
  });
}

function registerBriefingRoutes(app) {
  app.get("/api/trading/briefings", (req, res) => {
    let db;
    try {
      const hours = parseInt(req.query.hours) || 24;
      db = getTradingDb();
      const rows = db.prepare(
        "SELECT * FROM briefings WHERE created_at >= datetime('now', '-' || ? || ' hours') ORDER BY created_at DESC"
      ).all(hours);
      res.json(sanitizeForResponse(rows));
    } catch (err) {
      sendServerError(res, err);
    } finally {
      closeDb(db);
    }
  });
}

function registerTelegramMessageRoutes(app) {
  app.get("/api/trading/messages", (req, res) => {
    let db;
    try {
      const hours = parseInt(req.query.hours) || 24;
      const channel = req.query.channel;
      db = getTradingDb();
      let query = 'SELECT * FROM telegram_messages WHERE created_at >= datetime("now", "-" || ? || " hours")';
      const params = [hours];
      if (channel) {
        query += " AND channel_id = ?";
        params.push(channel);
      }
      query += " ORDER BY created_at DESC LIMIT 500";
      const rows = db.prepare(query).all(...params);
      res.json(sanitizeForResponse(rows));
    } catch (err) {
      sendServerError(res, err);
    } finally {
      closeDb(db);
    }
  });
}

function registerPriceAlertRoutes(app, priceMonitor) {
  app.get("/api/price-monitor/status", (req, res) => {
    res.json(sanitizeForResponse(priceMonitor.getStatus()));
  });

  app.get("/api/price-alerts", (req, res) => {
    let db;
    try {
      db = priceMonitor.getDb();
      const triggered = req.query.triggered;
      let query = "SELECT * FROM price_alerts";
      const params = [];
      if (triggered !== undefined) {
        query += " WHERE triggered = ?";
        params.push(triggered === "true" ? 1 : 0);
      }
      query += " ORDER BY symbol, target_price";
      const rows = db.prepare(query).all(...params);
      res.json(sanitizeForResponse(rows));
    } catch (err) {
      sendServerError(res, err);
    } finally {
      closeDb(db);
    }
  });

  app.post("/api/price-alerts", (req, res) => {
    let db;
    try {
      const { order_id, symbol, target_price, direction, alert_type, quantity, note } = req.body;
      if (!symbol || !target_price || !direction) {
        return res.status(400).json({ error: "Missing required: symbol, target_price, direction" });
      }
      db = priceMonitor.getDb();
      const result = db.prepare(
        `INSERT INTO price_alerts (order_id, symbol, target_price, direction, alert_type, quantity, note)
         VALUES (?, ?, ?, ?, ?, ?, ?)`
      ).run(order_id || null, symbol, target_price, direction, alert_type || "tp", quantity || 0, note || "");
      res.json({ ok: true, id: result.lastInsertRowid });
    } catch (err) {
      sendServerError(res, err);
    } finally {
      closeDb(db);
    }
  });

  app.delete("/api/price-alerts/:id", (req, res) => {
    let db;
    try {
      db = priceMonitor.getDb();
      const result = db.prepare("DELETE FROM price_alerts WHERE id = ?").run(req.params.id);
      if (result.changes === 0) {
        return res.status(404).json({ error: "Alert not found" });
      }
      res.json({ ok: true });
    } catch (err) {
      sendServerError(res, err);
    } finally {
      closeDb(db);
    }
  });

  app.delete("/api/price-alerts/order/:orderId", (req, res) => {
    let db;
    try {
      db = priceMonitor.getDb();
      const result = db.prepare("DELETE FROM price_alerts WHERE order_id = ?").run(req.params.orderId);
      res.json({ ok: true, deleted: result.changes });
    } catch (err) {
      sendServerError(res, err);
    } finally {
      closeDb(db);
    }
  });
}

function maskAccountConfig(row) {
  return {
    ...row,
    api_key: maskSecret(row.api_key),
    api_secret: maskSecret(row.api_secret),
  };
}

function normalizedText(value) {
  if (value === undefined || value === null || value === false) {
    return "";
  }
  return String(value).trim();
}

function normalizeAccountType(value) {
  const normalized = normalizedText(value).toLowerCase();
  if (!normalized) {
    return "main";
  }
  if (!ACCOUNT_TYPES.has(normalized)) {
    return false;
  }
  return normalized;
}

function normalizeTestnet(value, fallback) {
  if (value === undefined || value === null || value === "") {
    let fallbackValue = 0;
    if (fallback) {
      fallbackValue = 1;
    }
    return { ok: true, value: fallbackValue };
  }
  if (value === true || value === 1 || value === "1" || value === "true") {
    return { ok: true, value: 1 };
  }
  if (value === false || value === 0 || value === "0" || value === "false") {
    return { ok: true, value: 0 };
  }
  return { ok: false };
}

function normalizeEnabled(value, fallback) {
  return normalizeTestnet(value, fallback);
}

function normalizeRiskRatio(value, fallback) {
  let candidate = value;
  if (candidate === undefined || candidate === null || candidate === "") {
    candidate = fallback;
  }
  const numeric = Number(candidate);
  if (!Number.isFinite(numeric) || numeric < 0) {
    return { ok: false };
  }
  return { ok: true, value: numeric };
}

function normalizeRiskCapitalMultiplier(value, fallback) {
  let candidate = value;
  if (candidate === undefined) {
    candidate = fallback;
  }
  if (
    candidate === undefined
    || candidate === null
    || candidate === ""
    || typeof candidate === "boolean"
  ) {
    return { ok: false };
  }
  const numeric = Number(candidate);
  if (!Number.isFinite(numeric) || numeric <= 0) {
    return { ok: false };
  }
  return { ok: true, value: numeric };
}

function normalizeRiskCapitalAddon(value, fallback) {
  let candidate = value;
  if (candidate === undefined) {
    candidate = fallback;
  }
  if (
    candidate === undefined
    || candidate === null
    || candidate === ""
    || typeof candidate === "boolean"
  ) {
    return { ok: false };
  }
  const numeric = Number(candidate);
  if (!Number.isFinite(numeric) || numeric < 0) {
    return { ok: false };
  }
  return { ok: true, value: numeric };
}

function getAccount(db, accountId) {
  return db.prepare(`
    SELECT
      account_id,
      api_key,
      api_secret,
      default_risk_ratio,
      is_testnet,
      account_type,
      parent_account_id,
      execution_account_id,
      risk_capital_multiplier,
      risk_capital_addon,
      is_enabled
    FROM account_configs
    WHERE account_id = ?
  `).get(accountId);
}

function getChildAccountCount(db, accountId) {
  const result = db.prepare(`
    SELECT COUNT(*) AS count
    FROM account_configs
    WHERE account_type = 'subaccount'
      AND parent_account_id = ?
  `).get(accountId);
  return result.count;
}

function getAccountByExecutionId(db, executionAccountId) {
  return db.prepare(`
    SELECT account_id
    FROM account_configs
    WHERE execution_account_id = ?
  `).get(executionAccountId);
}

function validateAccountHierarchy(db, account) {
  const {
    accountId,
    accountType,
    parentAccountId,
    isTestnet,
  } = account;

  if (accountType === "main") {
    if (parentAccountId) {
      return {
        status: 400,
        error: "Main account cannot have parent_account_id",
      };
    }
    return false;
  }

  if (!parentAccountId) {
    return {
      status: 400,
      error: "Subaccount requires parent_account_id",
    };
  }
  if (parentAccountId === accountId) {
    return {
      status: 400,
      error: "Subaccount cannot reference itself",
    };
  }

  const parent = getAccount(db, parentAccountId);
  if (!parent) {
    return {
      status: 400,
      error: "Parent account not found",
    };
  }
  if (parent.account_type !== "main") {
    return {
      status: 400,
      error: "Parent account must be a main account",
    };
  }
  if (Number(parent.is_enabled) !== 1) {
    return {
      status: 400,
      error: "Parent account is disabled",
    };
  }
  if (Number(parent.is_testnet) !== Number(isTestnet)) {
    return {
      status: 400,
      error: "Subaccount environment must match its main account",
    };
  }
  return false;
}

function isDuplicateAccountError(err) {
  if (!err) {
    return false;
  }
  return err.code === "SQLITE_CONSTRAINT_PRIMARYKEY"
    || err.code === "SQLITE_CONSTRAINT_UNIQUE";
}

function sendServerError(res, err) {
  console.log("[trading-api] request failed:", safeErrorMessage(err));
  res.status(500).json({ error: "Internal server error" });
}

function closeDb(db) {
  if (db) {
    db.close();
  }
}

module.exports = {
  ensureTelegramMessagesTable,
  registerTradingApi,
  saveTelegramMessage,
  __test: {
    DEFAULT_TRADING_DB_PATH,
    resolveTradingDbPath,
    migrateAccountSchema,
    normalizeAccountType,
    normalizeEnabled,
    normalizeRiskCapitalAddon,
    normalizeRiskCapitalMultiplier,
    normalizeRiskRatio,
    normalizeTestnet,
    validateAccountHierarchy,
  },
};
