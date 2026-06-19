/**
 * Price Monitor — 监控活跃订单的止盈/止损价位
 * 
 * 从 trading.db 读取 price_alerts 表
 * 定期通过 Binance API 查价格
 * 到价时触发 trader agent 做订单管理
 */

const https = require("https");
const { execFile } = require("child_process");
const Database = require("better-sqlite3");
const path = require("path");
const os = require("os");
const { isEnabledByDefault } = require("./lib/env-flags");

const TRADING_DB_PATH = process.env.TRADING_DB_PATH || "/Users/balen/.openclaw/workspace-trader/trading.db";
const HERMES = process.env.HERMES_BIN || "/Users/balen/.hermes/hermes-agent/venv/bin/hermes";
const PRICE_MONITOR_ENABLED = isEnabledByDefault(process.env.PRICE_MONITOR_ENABLED);
const HERMES_ENV = {
  ...process.env,
  HERMES_PROFILE: process.env.HERMES_PROFILE || "trader",
  HERMES_ACCEPT_HOOKS: "1",
};

// Binance API base URLs
const BINANCE_FUTURES_API = "fapi.binance.com";
const BINANCE_TESTNET_API = "testnet.binancefuture.com";

// Poll interval (seconds)
const PRICE_CHECK_INTERVAL = 10;

// Use testnet by default (matches account config)
let useTestnet = true;

function getDb() {
  const db = new Database(TRADING_DB_PATH);
  db.pragma("journal_mode = WAL");
  return db;
}

/**
 * Ensure price_alerts table exists
 * 
 * Fields:
 *   id          - auto increment
 *   order_id    - FK to active_orders.id
 *   symbol      - e.g. BTCUSDT
 *   target_price - the price to watch for
 *   direction   - "above" (price >= target) or "below" (price <= target)
 *   alert_type  - "tp1", "tp2", "tp3", "tp4", "sl", "entry", etc.
 *   quantity    - how much to close/action at this price (optional)
 *   note        - free text for agent context
 *   triggered   - 0 or 1
 *   created_at  - timestamp
 */
function ensureTable() {
  const db = getDb();
  try {
    db.exec(`
      CREATE TABLE IF NOT EXISTS price_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER,
        symbol TEXT NOT NULL,
        target_price REAL NOT NULL,
        direction TEXT NOT NULL DEFAULT 'above',
        alert_type TEXT NOT NULL DEFAULT 'tp',
        quantity REAL DEFAULT 0,
        note TEXT DEFAULT '',
        triggered INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
      )
    `);
  } finally {
    db.close();
  }
}

/**
 * Get all active (untriggered) alerts, grouped by symbol
 */
function getActiveAlerts() {
  const db = getDb();
  try {
    const rows = db.prepare(
      "SELECT * FROM price_alerts WHERE triggered = 0 ORDER BY symbol, target_price"
    ).all();
    // Group by symbol
    const grouped = {};
    for (const row of rows) {
      if (!grouped[row.symbol]) grouped[row.symbol] = [];
      grouped[row.symbol].push(row);
    }
    return grouped;
  } finally {
    db.close();
  }
}

/**
 * Mark an alert as triggered
 */
function markTriggered(alertId) {
  const db = getDb();
  try {
    db.prepare("UPDATE price_alerts SET triggered = 1 WHERE id = ?").run(alertId);
  } finally {
    db.close();
  }
}

/**
 * Delete an alert
 */
function deleteAlert(alertId) {
  const db = getDb();
  try {
    db.prepare("DELETE FROM price_alerts WHERE id = ?").run(alertId);
  } finally {
    db.close();
  }
}

/**
 * Fetch current price from Binance futures API
 */
function fetchPrice(symbol) {
  const host = useTestnet ? BINANCE_TESTNET_API : BINANCE_FUTURES_API;
  return new Promise((resolve, reject) => {
    const options = {
      hostname: host,
      path: `/fapi/v1/ticker/price?symbol=${symbol}`,
      method: "GET",
      timeout: 5000,
    };

    const req = https.request(options, (res) => {
      let data = "";
      res.on("data", (chunk) => (data += chunk));
      res.on("end", () => {
        try {
          const parsed = JSON.parse(data);
          if (parsed.price) {
            resolve(parseFloat(parsed.price));
          } else {
            reject(new Error(`No price in response: ${data.substring(0, 100)}`));
          }
        } catch (e) {
          reject(e);
        }
      });
    });
    req.on("error", reject);
    req.on("timeout", () => {
      req.destroy();
      reject(new Error("timeout"));
    });
    req.end();
  });
}

/**
 * Fetch multiple prices in one request (batch)
 */
function fetchAllPrices() {
  const host = useTestnet ? BINANCE_TESTNET_API : BINANCE_FUTURES_API;
  return new Promise((resolve, reject) => {
    const options = {
      hostname: host,
      path: "/fapi/v1/ticker/price",
      method: "GET",
      timeout: 5000,
    };

    const req = https.request(options, (res) => {
      let data = "";
      res.on("data", (chunk) => (data += chunk));
      res.on("end", () => {
        try {
          const arr = JSON.parse(data);
          const priceMap = {};
          for (const item of arr) {
            priceMap[item.symbol] = parseFloat(item.price);
          }
          resolve(priceMap);
        } catch (e) {
          reject(e);
        }
      });
    });
    req.on("error", reject);
    req.on("timeout", () => {
      req.destroy();
      reject(new Error("timeout"));
    });
    req.end();
  });
}

/**
 * Trigger trader agent via Hermes cron
 */
function triggerTrader(alert, currentPrice) {
  if (!PRICE_MONITOR_ENABLED) {
    console.log("[price-monitor] Hermes price monitor disabled");
    return false;
  }

  const message = `⚠️ 价格警报触发！

品种：${alert.symbol}
当前价：${currentPrice}
目标价：${alert.target_price}
类型：${alert.alert_type}
方向：${alert.direction === "above" ? "上穿" : "下穿"}
订单ID：#${alert.order_id}
${alert.quantity ? `操作数量：${alert.quantity}` : ""}
${alert.note ? `备注：${alert.note}` : ""}

请按照 crypto-trader skill 流程执行订单管理：
1. 查询订单 #${alert.order_id} 的当前状态和持仓
2. 根据警报类型执行操作（${alert.alert_type} → 部分平仓/全部平仓/移动止损等）
3. 用 message 工具通过 accountId=trader channel=telegram target=telegram:balen 发送操作结果`;

  const jobName = `price-alert-${alert.id}-${alert.symbol}`;
  const addArgs = [
    "cron", "create",
    new Date().toISOString(),
    message,
    "--name", jobName,
    "--deliver", "local",
    "--repeat", "1",
    "--skill", "crypto-trader",
    "--workdir", "/Users/balen/.openclaw/workspace-trader",
  ];

  execFile(HERMES, addArgs, { timeout: 30000, env: HERMES_ENV }, (err, stdout, stderr) => {
    if (err) {
      console.log(`[price-monitor] hermes cron create error: ${err.message}`);
      if (stderr) console.log(`[price-monitor] hermes stderr: ${String(stderr).substring(0, 300)}`);
      return;
    }
    const match = stdout.match(/Created job:\s*(\S+)/);
    const jobId = match && match[1];
    if (!jobId) {
      console.log(`[price-monitor] No job ID in hermes output: ${stdout.substring(0, 300)}`);
      return;
    }
    console.log(`[price-monitor] hermes cron job created: ${jobId}`);

    execFile(HERMES, ["cron", "run", jobId, "--accept-hooks"], { timeout: 10000, env: HERMES_ENV }, (runErr, runOut, runStderr) => {
      if (runErr) {
        console.log(`[price-monitor] hermes cron run error: ${runErr.message}`);
        if (runStderr) console.log(`[price-monitor] hermes run stderr: ${String(runStderr).substring(0, 300)}`);
        return;
      }
      console.log(`[price-monitor] hermes cron run queued: ${jobId}`);
    });
  });
  return true;
}

/**
 * Check if price has hit any alert targets
 */
function checkAlert(alert, currentPrice) {
  if (alert.direction === "above" && currentPrice >= alert.target_price) {
    return true;
  }
  if (alert.direction === "below" && currentPrice <= alert.target_price) {
    return true;
  }
  return false;
}

/**
 * Main price check loop
 */
let running = false;

async function checkPrices() {
  if (running) return;
  running = true;

  try {
    const alertsBySymbol = getActiveAlerts();
    const symbols = Object.keys(alertsBySymbol);
    if (symbols.length === 0) {
      running = false;
      return;
    }

    // Fetch all prices at once
    const prices = await fetchAllPrices();

    for (const symbol of symbols) {
      const currentPrice = prices[symbol];
      if (!currentPrice) {
        console.log(`[price-monitor] No price for ${symbol}`);
        continue;
      }

      for (const alert of alertsBySymbol[symbol]) {
        if (checkAlert(alert, currentPrice)) {
          console.log(
            `[price-monitor] 🚨 TRIGGERED: ${symbol} ${alert.alert_type} @ ${alert.target_price} (current: ${currentPrice})`
          );
          markTriggered(alert.id);
          triggerTrader(alert, currentPrice);
        }
      }
    }
  } catch (err) {
    console.log(`[price-monitor] Error: ${err.message}`);
  }

  running = false;
}

/**
 * Start the price monitor
 */
let intervalHandle = null;

function start() {
  if (!PRICE_MONITOR_ENABLED) {
    console.log("[price-monitor] Disabled");
    return;
  }

  ensureTable();
  console.log(`[price-monitor] Started, checking every ${PRICE_CHECK_INTERVAL}s`);

  // Check account to determine testnet or not
  try {
    const db = getDb();
    const row = db.prepare("SELECT is_testnet FROM account_configs LIMIT 1").get();
    if (row) useTestnet = !!row.is_testnet;
    db.close();
  } catch {}

  console.log(`[price-monitor] Using ${useTestnet ? "testnet" : "mainnet"} API`);

  // Initial check
  checkPrices();
  // Start interval
  intervalHandle = setInterval(checkPrices, PRICE_CHECK_INTERVAL * 1000);
}

function stop() {
  if (intervalHandle) {
    clearInterval(intervalHandle);
    intervalHandle = null;
  }
  console.log("[price-monitor] Stopped");
}

/**
 * Get monitor status
 */
function getStatus() {
  const alerts = getActiveAlerts();
  const totalAlerts = Object.values(alerts).reduce((sum, arr) => sum + arr.length, 0);
  return {
    enabled: PRICE_MONITOR_ENABLED,
    running: !!intervalHandle,
    interval: PRICE_CHECK_INTERVAL,
    testnet: useTestnet,
    activeAlerts: totalAlerts,
    symbols: Object.keys(alerts),
  };
}

module.exports = {
  start,
  stop,
  getStatus,
  ensureTable,
  getDb: getDb,
  __test: {
    triggerTrader,
  },
};
