const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const { createWatchedEntryHandler } = require("../lib/watched-entry-routing");

function createRecorder(env) {
  const calls = [];
  let importerCalls = 0;
  let approvedWrites = 0;
  const handler = createWatchedEntryHandler({
    env,
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
  });

  return { calls, handler, getImporterCalls: () => importerCalls, getApprovedWrites: () => approvedWrites };
}

test("watched entry routing defaults to collection only", () => {
  const { calls, handler } = createRecorder({});

  handler({ id: 11 });

  assert.deepEqual(calls, [
    ["push", 11],
    ["save", 11],
  ]);
});

test("watched entry routing stays collection only when legacy flag is enabled", () => {
  const { calls, handler } = createRecorder({
    HERMES_TRADER_CRON_ENABLED: "1",
  });

  handler({ id: 12 });

  assert.deepEqual(calls, [
    ["push", 12],
    ["save", 12],
  ]);
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

test("watcher server keeps direct Hermes trader cron detached", () => {
  const serverPath = path.join(__dirname, "..", "server.js");
  const source = fs.readFileSync(serverPath, "utf8");

  assert.doesNotMatch(source, /lib\/hermes-cron/);
  assert.doesNotMatch(source, /triggerHermesCron/);
  assert.doesNotMatch(source, /traderCronForwarder:/);
});

test("watcher source contains no direct Hermes execution path", () => {
  const watcherRoot = path.join(__dirname, "..");
  const sources = [
    "server.js",
    "price-monitor.js",
    path.join("lib", "watched-entry-routing.js"),
  ].map((file) => {
    return fs.readFileSync(path.join(watcherRoot, file), "utf8");
  }).join("\n");

  assert.doesNotMatch(sources, /hermes-agent|crypto-trader|cron", "run/);
  assert.doesNotMatch(sources, /execFile\s*\(/);
});
