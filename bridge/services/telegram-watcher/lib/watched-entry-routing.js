const { isEnabledByDefault } = require("./env-flags");
const { allowlistedLogToken } = require("./safe-log");

function createWatchedEntryHandler(options) {
  const {
    pushMessage,
    saveTelegramMessage,
    importSignalToFreqtrade,
    forwardToTrader,
    env = process.env,
    logger = console,
  } = options;
  const traderCronEnabled = isEnabledByDefault(env.HERMES_TRADER_CRON_ENABLED);

  return function handleWatchedEntry(entry) {
    pushMessage(entry);
    saveTelegramMessage(entry);
    try {
      importSignalToFreqtrade(entry);
    } catch (err) {
      const code = allowlistedLogToken(err.code || err.name, "error");
      logger.log(`[signal-importer] failed: ${code}`);
    }

    if (!traderCronEnabled) {
      logger.log("[forward] Hermes trader cron disabled");
      return;
    }

    forwardToTrader(entry);
  };
}

module.exports = {
  createWatchedEntryHandler,
};
