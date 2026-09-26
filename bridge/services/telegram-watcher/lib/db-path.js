const DEFAULT_TRADING_DB_PATH = "/var/lib/docker/volumes/trader_signal-data/_data/watcher-trading.db";
const CANONICAL_TRADING_DB_ENV = "TRADER_TRADING_DB_PATH";
const LEGACY_TRADING_DB_ENVS = ["WATCHER_TRADING_DB", "TRADING_DB_PATH"];

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

module.exports = { DEFAULT_TRADING_DB_PATH, resolveTradingDbPath };
