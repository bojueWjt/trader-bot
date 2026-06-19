const { isExplicitlyEnabled } = require("./env-flags");

function createWatchedEntryHandler(options) {
  const {
    pushMessage,
    saveTelegramMessage,
    traderCronForwarder,
    env = process.env,
    logger = console,
  } = options;
  const traderCronEnabled = isExplicitlyEnabled(env.HERMES_TRADER_CRON_ENABLED);

  return function handleWatchedEntry(entry) {
    pushMessage(entry);
    saveTelegramMessage(entry);

    if (!traderCronEnabled) {
      logger.log("[forward] Hermes trader cron disabled");
      return;
    }

    if (typeof traderCronForwarder === "function") {
      traderCronForwarder(entry);
    }
  };
}

module.exports = {
  createWatchedEntryHandler,
};
