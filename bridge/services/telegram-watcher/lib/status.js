const Database = require("better-sqlite3");

const { resolveTradingDbPath } = require("./db-path");

function readLastMessageIngestedAt(env = process.env) {
  const dbPath = resolveTradingDbPath(env);
  let db;
  try {
    db = new Database(dbPath, { readonly: true, fileMustExist: true });
    db.pragma("busy_timeout = 5000");
    const row = db.prepare("SELECT max(created_at) AS latest FROM telegram_messages").get();
    if (row && row.latest === null) {
      return null;
    }
    const parsed = new Date(`${row.latest.replace(" ", "T")}Z`);
    if (Number.isNaN(parsed.getTime())) {
      throw new Error("invalid message timestamp");
    }
    return parsed.toISOString();
  } catch {
    throw new Error("message database unreadable");
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
