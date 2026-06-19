const Database = require("better-sqlite3");
const {
  maskSecret,
  safeErrorMessage,
  sanitizeForResponse,
} = require("./safe-log");

const TRADING_DB_PATH = process.env.TRADING_DB_PATH || "/Users/balen/.openclaw/workspace-trader/trading.db";

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
    is_testnet          INTEGER DEFAULT 1
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
  } catch (err) {
    console.log("[db] Failed to ensure trading tables:", safeErrorMessage(err));
  } finally {
    closeDb(db);
  }
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
      const rows = db.prepare("SELECT * FROM account_configs").all();
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
      const { account_id, api_key, api_secret, default_risk_ratio, is_testnet } = req.body;
      if (!account_id || !api_key || !api_secret) {
        return res.status(400).json({ error: "Missing required fields: account_id, api_key, api_secret" });
      }
      let defaultRiskRatio = default_risk_ratio;
      if (defaultRiskRatio === undefined || defaultRiskRatio === null) {
        defaultRiskRatio = 0.01;
      }
      const riskVal = Number(defaultRiskRatio);
      if (!isFinite(riskVal) || riskVal < 0) {
        return res.status(400).json({ error: "default_risk_ratio must be a non-negative number" });
      }
      db = getTradingDb();
      db.prepare(
        "INSERT INTO account_configs (account_id, api_key, api_secret, default_risk_ratio, is_testnet) VALUES (?, ?, ?, ?, ?)"
      ).run(account_id, api_key, api_secret, riskVal, is_testnet ? 1 : 0);
      res.json({ ok: true, account_id });
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
      const result = db.prepare("DELETE FROM account_configs WHERE account_id = ?").run(req.params.id);
      if (result.changes === 0) {
        return res.status(404).json({ error: "Account not found" });
      }
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
      const rows = db.prepare("SELECT * FROM channel_routing").all();
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
      const { channel_id, target_account_id, channel_name } = req.body;
      if (!channel_id || !target_account_id) {
        return res.status(400).json({ error: "Missing required fields: channel_id, target_account_id" });
      }
      db = getTradingDb();
      db.prepare(
        "INSERT OR REPLACE INTO channel_routing (channel_id, target_account_id, channel_name) VALUES (?, ?, ?)"
      ).run(channel_id, target_account_id, channel_name || "");
      res.json({ ok: true, channel_id, target_account_id });
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
};
