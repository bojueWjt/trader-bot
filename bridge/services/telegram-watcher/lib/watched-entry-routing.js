function createWatchedEntryHandler(options) {
  const {
    pushMessage,
    saveTelegramMessage,
  } = options;

  return function handleWatchedEntry(entry) {
    pushMessage(entry);
    saveTelegramMessage(entry);
  };
}

module.exports = {
  createWatchedEntryHandler,
};
