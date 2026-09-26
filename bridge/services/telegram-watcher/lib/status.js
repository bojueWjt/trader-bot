const Database = require("better-sqlite3");

const DEFAULT_TRADING_DB_PATH = "/var/lib/docker/volumes/trader_signal-data/_data/watcher-trading.db";

function readLastMessageIngestedAt(env = process.env) {
  const dbPath = String(env.TRADER_TRADING_DB_PATH || env.WATCHER_TRADING_DB || env.TRADING_DB_PATH || DEFAULT_TRADING_DB_PATH).trim();
  let db;
  try {
    db = new Database(dbPath, { readonly: true, fileMustExist: true });
    const row = db.prepare("SELECT max(created_at) AS latest FROM telegram_messages").get();
    if (!row || !row.latest) {
      return null;
    }
    const parsed = new Date(`${row.latest.replace(" ", "T")}Z`);
    if (Number.isNaN(parsed.getTime())) {
      return null;
    }
    return parsed.toISOString();
  } catch {
    return null;
  } finally {
    if (db) {
      db.close();
    }
  }
}

function buildStatus(config, state, env = process.env) {
  const configured = Boolean(config.apiId && config.apiHash);
  const loggedIn = Boolean(config.session);
  const connected = Boolean(state.connected);
  let connection = "disconnected";
  if (!configured || !loggedIn) {
    connection = "needs_login";
  } else if (connected) {
    connection = "connected";
  }
  return {
    configured,
    loggedIn,
    connected,
    watchGroups: config.watchGroups || [],
    observed_at: new Date(state.now).toISOString(),
    connection,
    listener: state.listening ? "listening" : "stopped",
    last_telegram_activity_at: state.lastTelegramActivityAt ? new Date(state.lastTelegramActivityAt).toISOString() : null,
    last_message_ingested_at: readLastMessageIngestedAt(env),
  };
}

module.exports = { buildStatus, readLastMessageIngestedAt };
