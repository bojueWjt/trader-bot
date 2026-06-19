const assert = require("node:assert/strict");
const test = require("node:test");

const { createWatchedEntryHandler } = require("../lib/watched-entry-routing");

function createRecorder(env) {
  const calls = [];
  const logs = [];
  const handler = createWatchedEntryHandler({
    env,
    logger: {
      log(line) {
        logs.push(String(line));
      },
    },
    pushMessage(entry) {
      calls.push(["push", entry.id]);
    },
    saveTelegramMessage(entry) {
      calls.push(["save", entry.id]);
    },
    importSignalToFreqtrade(entry) {
      calls.push(["import", entry.id]);
    },
    forwardToTrader(entry) {
      calls.push(["forward", entry.id]);
    },
  });

  return { calls, handler, logs };
}

test("watched entry routing defaults to importer and trader forwarder", () => {
  const { calls, handler } = createRecorder({});

  handler({ id: 11 });

  assert.deepEqual(calls, [
    ["push", 11],
    ["save", 11],
    ["import", 11],
    ["forward", 11],
  ]);
});

test("watched entry routing can run importer-only migration mode", () => {
  const { calls, handler, logs } = createRecorder({
    HERMES_TRADER_CRON_ENABLED: "0",
  });

  handler({ id: 12 });

  assert.deepEqual(calls, [
    ["push", 12],
    ["save", 12],
    ["import", 12],
  ]);
  assert.deepEqual(logs, ["[forward] Hermes trader cron disabled"]);
});

test("watched entry routing keeps trader forwarder active after importer failure", () => {
  const calls = [];
  const logs = [];
  const handler = createWatchedEntryHandler({
    env: {},
    logger: {
      log(line) {
        logs.push(String(line));
      },
    },
    pushMessage(entry) {
      calls.push(["push", entry.id]);
    },
    saveTelegramMessage(entry) {
      calls.push(["save", entry.id]);
    },
    importSignalToFreqtrade(entry) {
      calls.push(["import", entry.id]);
      const err = new Error("import failed");
      err.code = "TEST_FAILURE";
      throw err;
    },
    forwardToTrader(entry) {
      calls.push(["forward", entry.id]);
    },
  });

  handler({ id: 13 });

  assert.deepEqual(calls, [
    ["push", 13],
    ["save", 13],
    ["import", 13],
    ["forward", 13],
  ]);
  assert.deepEqual(logs, ["[signal-importer] failed: TEST_FAILURE"]);
});
