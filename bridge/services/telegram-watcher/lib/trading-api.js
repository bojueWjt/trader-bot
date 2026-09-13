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
    db.exec(TRADING_SCHEMA_SQL);
    migrateAccountSchema(db);
  } catch (err) {
    console.log("[db] Failed to ensure trading tables:", safeErrorMessage(err));
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
  registerAccountRoutes(app);
  registerChannelRoutes(app);
  registerRiskRoutes(app);
  registerOrderRoutes(app);
  registerBriefingRoutes(app);
  registerTelegramMessageRoutes(app);
  registerPriceAlertRoutes(app, priceMonitor);
}

function registerAccountRoutes(app) {
  app.get("/api/trading/accounts", (req, res) => {
    let db;
    try {
      db = getTradingDb();
      const rows = db.prepare(`
        SELECT
          account.*,
          (
            SELECT COUNT(*)
            FROM channel_routing AS route
            WHERE route.target_account_id = account.account_id
          ) AS channel_count,
          (
            SELECT COUNT(*)
            FROM account_configs AS child
            WHERE child.account_type = 'subaccount'
              AND child.parent_account_id = account.account_id
          ) AS subaccount_count
        FROM account_configs AS account
        ORDER BY
          CASE
            WHEN account.account_type = 'main' THEN account.account_id
            ELSE account.parent_account_id
          END,
          CASE account.account_type WHEN 'main' THEN 0 ELSE 1 END,
          account.account_id
      `).all();
      const masked = rows.map((row) => {
        return maskAccountConfig(row);
      });
      res.json(sanitizeForResponse(masked));
    } catch (err) {
      sendServerError(res, err);
    } finally {
      closeDb(db);
    }
  });

  app.post("/api/trading/accounts", (req, res) => {
    let db;
    try {
      const body = req.body || {};
      const accountId = normalizedText(body.account_id);
      const apiKey = normalizedText(body.api_key);
      const apiSecret = normalizedText(body.api_secret);
      const accountType = normalizeAccountType(body.account_type);
      const parentAccountId = normalizedText(body.parent_account_id);
      let executionAccountId = normalizedText(body.execution_account_id);
      if (!executionAccountId) {
        executionAccountId = accountId;
      }
      const testnetResult = normalizeTestnet(body.is_testnet, false);
      const riskResult = normalizeRiskRatio(body.default_risk_ratio, 0.01);
      const multiplierResult = normalizeRiskCapitalMultiplier(
        body.risk_capital_multiplier,
        1
      );
      const addonResult = normalizeRiskCapitalAddon(
        body.risk_capital_addon
      );
      const enabledResult = normalizeEnabled(body.is_enabled, true);

      if (!accountId || !apiKey || !apiSecret) {
        return res.status(400).json({ error: "Missing required fields: account_id, api_key, api_secret" });
      }
      if (!CREDENTIAL_ACCOUNT_ID_PATTERN.test(accountId)) {
        return res.status(400).json({ error: "account_id must be 1-128 printable characters" });
      }
      if (!EXECUTION_ACCOUNT_ID_PATTERN.test(executionAccountId)) {
        return res.status(400).json({ error: "execution_account_id must use letters, numbers, dots, underscores, or hyphens" });
      }
      if (!accountType) {
        return res.status(400).json({ error: "account_type must be main or subaccount" });
      }
      if (!testnetResult.ok) {
        return res.status(400).json({ error: "is_testnet must be a boolean" });
      }
      if (!riskResult.ok) {
        return res.status(400).json({ error: "default_risk_ratio must be a non-negative number" });
      }
      if (!multiplierResult.ok) {
        return res.status(400).json({
          error: "risk_capital_multiplier must be greater than 0",
        });
      }
      if (!addonResult.ok) {
        return res.status(400).json({
          error: "risk_capital_addon is required and must be >= 0",
        });
      }
      if (!enabledResult.ok) {
        return res.status(400).json({ error: "is_enabled must be a boolean" });
      }

      db = getTradingDb();
      const existingExecutionAccount = getAccountByExecutionId(
        db,
        executionAccountId
      );
      if (existingExecutionAccount) {
        return res.status(409).json({ error: "V3 execution account already exists" });
      }
      const isTestnet = testnetResult.value;
      const hierarchyError = validateAccountHierarchy(db, {
        accountId,
        accountType,
        parentAccountId,
        isTestnet,
      });
      if (hierarchyError) {
        return res.status(hierarchyError.status).json({ error: hierarchyError.error });
      }

      db.prepare(`
        INSERT INTO account_configs (
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
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      `).run(
        accountId,
        apiKey,
        apiSecret,
        riskResult.value,
        isTestnet,
        accountType,
        parentAccountId,
        executionAccountId,
        multiplierResult.value,
        addonResult.value,
        enabledResult.value
      );
      res.json({ ok: true, account_id: accountId });
    } catch (err) {
      if (isDuplicateAccountError(err)) {
        return res.status(409).json({ error: "Account already exists" });
      }
      sendServerError(res, err);
    } finally {
      closeDb(db);
    }
  });

  app.put("/api/trading/accounts/:id", (req, res) => {
    let db;
    try {
      const accountId = normalizedText(req.params.id);
      const body = req.body || {};
      db = getTradingDb();
      const current = getAccount(db, accountId);
      if (!current) {
        return res.status(404).json({ error: "Account not found" });
      }

      let accountType = current.account_type;
      if (body.account_type !== undefined) {
        accountType = normalizeAccountType(body.account_type);
      }
      if (!accountType) {
        return res.status(400).json({ error: "account_type must be main or subaccount" });
      }

      let parentAccountId = current.parent_account_id || "";
      if (body.parent_account_id !== undefined) {
        parentAccountId = normalizedText(body.parent_account_id);
      }
      if (accountType === "main") {
        parentAccountId = "";
      }
      let executionAccountId = current.execution_account_id || current.account_id;
      if (body.execution_account_id !== undefined) {
        executionAccountId = normalizedText(body.execution_account_id);
      }
      if (!EXECUTION_ACCOUNT_ID_PATTERN.test(executionAccountId)) {
        return res.status(400).json({ error: "execution_account_id must use letters, numbers, dots, underscores, or hyphens" });
      }
      const existingExecutionAccount = getAccountByExecutionId(
        db,
        executionAccountId
      );
      if (
        existingExecutionAccount
        && existingExecutionAccount.account_id !== accountId
      ) {
        return res.status(409).json({ error: "V3 execution account already exists" });
      }

      const testnetResult = normalizeTestnet(body.is_testnet, Boolean(current.is_testnet));
      if (!testnetResult.ok) {
        return res.status(400).json({ error: "is_testnet must be a boolean" });
      }
      const isTestnet = testnetResult.value;
      const riskResult = normalizeRiskRatio(body.default_risk_ratio, current.default_risk_ratio);
      if (!riskResult.ok) {
        return res.status(400).json({ error: "default_risk_ratio must be a non-negative number" });
      }
      const multiplierResult = normalizeRiskCapitalMultiplier(
        body.risk_capital_multiplier,
        current.risk_capital_multiplier
      );
      if (!multiplierResult.ok) {
        return res.status(400).json({ error: "risk_capital_multiplier must be greater than 0" });
      }
      const addonResult = normalizeRiskCapitalAddon(
        body.risk_capital_addon,
        current.risk_capital_addon
      );
      const enabledResult = normalizeEnabled(
        body.is_enabled,
        Number(current.is_enabled) === 1
      );
      if (!enabledResult.ok) {
        return res.status(400).json({ error: "is_enabled must be a boolean" });
      }
      const isEnabled = enabledResult.value;
      if (
        Number(current.is_enabled) !== 1
        && isEnabled === 1
        && !addonResult.ok
      ) {
        return res.status(400).json({
          error: "enabling an account requires risk_capital_addon to be >= 0",
        });
      }
      if (!addonResult.ok) {
        return res.status(400).json({ error: "risk_capital_addon must be >= 0" });
      }

      let apiKey = current.api_key;
      const requestedApiKey = normalizedText(body.api_key);
      if (requestedApiKey) {
        apiKey = requestedApiKey;
      }
      let apiSecret = current.api_secret;
      const requestedApiSecret = normalizedText(body.api_secret);
      if (requestedApiSecret) {
        apiSecret = requestedApiSecret;
      }

      const childCount = getChildAccountCount(db, accountId);
      if (accountType === "subaccount" && childCount > 0) {
        return res.status(409).json({ error: "Move or remove child accounts before changing this main account" });
      }
      if (accountType === "main" && childCount > 0) {
        const mismatchedChild = db.prepare(`
          SELECT account_id
          FROM account_configs
          WHERE parent_account_id = ?
            AND account_type = 'subaccount'
            AND is_testnet != ?
          LIMIT 1
        `).get(accountId, isTestnet);
        if (mismatchedChild) {
          return res.status(409).json({ error: "Main account environment must match all child accounts" });
        }
      }

      const hierarchyError = validateAccountHierarchy(db, {
        accountId,
        accountType,
        parentAccountId,
        isTestnet,
      });
      if (hierarchyError) {
        return res.status(hierarchyError.status).json({ error: hierarchyError.error });
      }

      db.prepare(`
        UPDATE account_configs
        SET api_key = ?,
            api_secret = ?,
            default_risk_ratio = ?,
            is_testnet = ?,
            account_type = ?,
            parent_account_id = ?,
            execution_account_id = ?,
            risk_capital_multiplier = ?,
            risk_capital_addon = ?,
            is_enabled = ?
        WHERE account_id = ?
      `).run(
        apiKey,
        apiSecret,
        riskResult.value,
        isTestnet,
        accountType,
        parentAccountId,
        executionAccountId,
        multiplierResult.value,
        addonResult.value,
        isEnabled,
        accountId
      );
      res.json({ ok: true, account_id: accountId });
    } catch (err) {
      sendServerError(res, err);
    } finally {
      closeDb(db);
    }
  });

  app.delete("/api/trading/accounts/:id", (req, res) => {
    let db;
    try {
      db = getTradingDb();
      const accountId = normalizedText(req.params.id);
      const account = getAccount(db, accountId);
      if (!account) {
        return res.status(404).json({ error: "Account not found" });
      }

      const childCount = getChildAccountCount(db, accountId);
      if (childCount > 0) {
        return res.status(409).json({
          error: "Account has subaccounts",
          subaccount_count: childCount,
        });
      }

      const channelCount = db.prepare(`
        SELECT COUNT(*) AS count
        FROM channel_routing
        WHERE target_account_id = ?
      `).get(accountId).count;
      if (channelCount > 0) {
        return res.status(409).json({
          error: "Account is used by channel routing",
          channel_count: channelCount,
        });
      }

      const placeholders = ACTIVE_ORDER_STATUSES.map(() => "?").join(", ");
      const activeOrderCount = db.prepare(`
        SELECT COUNT(*) AS count
        FROM active_orders
        WHERE account_id = ?
          AND status IN (${placeholders})
      `).get(accountId, ...ACTIVE_ORDER_STATUSES).count;
      if (activeOrderCount > 0) {
        return res.status(409).json({
          error: "Account has active orders",
          active_order_count: activeOrderCount,
        });
      }

      db.prepare("DELETE FROM account_configs WHERE account_id = ?").run(accountId);
      res.json({ ok: true });
    } catch (err) {
      sendServerError(res, err);
    } finally {
      closeDb(db);
    }
  });
}

function registerChannelRoutes(app) {
  app.get("/api/trading/channels", (req, res) => {
    let db;
    try {
      db = getTradingDb();
      const rows = db.prepare(`
        SELECT
          route.channel_id,
          route.target_account_id,
          route.channel_name,
          account.account_type AS target_account_type,
          account.parent_account_id,
          account.execution_account_id,
          account.is_enabled AS target_is_enabled
        FROM channel_routing AS route
        LEFT JOIN account_configs AS account
          ON account.account_id = route.target_account_id
        ORDER BY route.channel_name, route.channel_id
      `).all();
      res.json(sanitizeForResponse(rows));
    } catch (err) {
      sendServerError(res, err);
    } finally {
      closeDb(db);
    }
  });

  app.post("/api/trading/channels", (req, res) => {
    let db;
    try {
      const body = req.body || {};
      const channelId = normalizedText(body.channel_id);
      const targetAccountId = normalizedText(body.target_account_id);
      const channelName = normalizedText(body.channel_name);
      if (!channelId || !targetAccountId) {
        return res.status(400).json({ error: "Missing required fields: channel_id, target_account_id" });
      }
      db = getTradingDb();
      const targetAccount = getAccount(db, targetAccountId);
      if (!targetAccount) {
        return res.status(400).json({ error: "Target account not found" });
      }
      if (Number(targetAccount.is_enabled) !== 1) {
        return res.status(400).json({ error: "Target account is disabled" });
      }
      db.prepare(`
        INSERT INTO channel_routing (channel_id, target_account_id, channel_name)
        VALUES (?, ?, ?)
        ON CONFLICT(channel_id) DO UPDATE SET
          target_account_id = excluded.target_account_id,
          channel_name = excluded.channel_name
      `).run(channelId, targetAccountId, channelName);
      res.json({
        ok: true,
        channel_id: channelId,
        target_account_id: targetAccountId,
      });
    } catch (err) {
      sendServerError(res, err);
    } finally {
      closeDb(db);
    }
  });

  app.delete("/api/trading/channels/:id", (req, res) => {
    let db;
    try {
      db = getTradingDb();
      const result = db.prepare("DELETE FROM channel_routing WHERE channel_id = ?").run(req.params.id);
      if (result.changes === 0) {
        return res.status(404).json({ error: "Channel routing not found" });
      }
      res.json({ ok: true });
    } catch (err) {
      sendServerError(res, err);
    } finally {
      closeDb(db);
    }
  });
}

function registerRiskRoutes(app) {
  app.get("/api/trading/risks", (req, res) => {
    let db;
    try {
      db = getTradingDb();
      const rows = db.prepare("SELECT * FROM symbol_risk_configs").all();
      res.json(sanitizeForResponse(rows));
    } catch (err) {
      sendServerError(res, err);
    } finally {
      closeDb(db);
    }
  });

  app.post("/api/trading/risks", (req, res) => {
    let db;
    try {
      const { symbol, risk_ratio } = req.body;
      if (!symbol || risk_ratio === undefined) {
        return res.status(400).json({ error: "Missing required fields: symbol, risk_ratio" });
      }
      const riskVal = Number(risk_ratio);
      if (!isFinite(riskVal) || riskVal < 0) {
        return res.status(400).json({ error: "risk_ratio must be a non-negative number" });
      }
      db = getTradingDb();
      db.prepare(
        "INSERT OR REPLACE INTO symbol_risk_configs (symbol, risk_ratio) VALUES (?, ?)"
      ).run(symbol, riskVal);
      res.json({ ok: true, symbol, risk_ratio });
    } catch (err) {
      sendServerError(res, err);
    } finally {
      closeDb(db);
    }
  });

  app.delete("/api/trading/risks/:symbol", (req, res) => {
    let db;
    try {
      db = getTradingDb();
      const result = db.prepare("DELETE FROM symbol_risk_configs WHERE symbol = ?").run(req.params.symbol);
      if (result.changes === 0) {
        return res.status(404).json({ error: "Risk config not found" });
      }
      res.json({ ok: true });
    } catch (err) {
      sendServerError(res, err);
    } finally {
      closeDb(db);
    }
  });
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
