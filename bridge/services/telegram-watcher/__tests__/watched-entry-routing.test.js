const assert = require("node:assert/strict");
const test = require("node:test");

const { createWatchedEntryHandler } = require("../lib/watched-entry-routing");

function createRecorder(env) {
  const calls = [];
  const logs = [];
  let importerCalls = 0;
  let approvedWrites = 0;
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
      if (entry.approved) {
        approvedWrites += 1;
      }
    },
    importSignalToFreqtrade(entry) {
      importerCalls += 1;
    },
    traderCronForwarder(entry) {
      calls.push(["forward", entry.id]);
    },
  });

  return { calls, handler, logs, getImporterCalls: () => importerCalls, getApprovedWrites: () => approvedWrites };
}

test("watched entry routing defaults to collection only", () => {
  const { calls, handler, logs } = createRecorder({});

  handler({ id: 11 });

  assert.deepEqual(calls, [
    ["push", 11],
    ["save", 11],
  ]);
  assert.deepEqual(logs, ["[forward] Hermes trader cron disabled"]);
});

test("watched entry routing requires explicit Hermes cron enablement to forward", () => {
  const { calls, handler, logs } = createRecorder({
    HERMES_TRADER_CRON_ENABLED: "1",
  });

  handler({ id: 12 });

  assert.deepEqual(calls, [
    ["push", 12],
    ["save", 12],
    ["forward", 12],
  ]);
  assert.deepEqual(logs, []);
});

test("default disabled importer and Hermes path add zero risk commands", () => {
  const { calls, handler, getImporterCalls, getApprovedWrites } = createRecorder({});

  handler({ id: 13, text: "BTC long entry 65000 stop 64000 take profit 67000" });

  assert.deepEqual(calls, [
    ["push", 13],
    ["save", 13],
  ]);
  assert.equal(getImporterCalls(), 0);
  assert.equal(getApprovedWrites(), 0);
});
