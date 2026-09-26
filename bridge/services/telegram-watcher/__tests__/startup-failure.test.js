const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");

test("trading database initialization failure exits process nonzero despite exception guard", () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "watcher-init-failure-"));
  const modulePath = require.resolve("../lib/trading-api");
  const script = `
    process.on("uncaughtException", () => {});
    const express = require(${JSON.stringify(require.resolve("express"))});
    const { registerTradingApi } = require(${JSON.stringify(modulePath)});
    registerTradingApi(express(), { getDb() { throw new Error("must not reach"); }, getStatus() { return {}; } });
  `;
  try {
    const result = spawnSync(process.execPath, ["-e", script], {
      env: {
        ...process.env,
        TRADER_TRADING_DB_PATH: directory,
        WATCHER_TRADING_DB: "",
        TRADING_DB_PATH: "",
      },
      encoding: "utf8",
      timeout: 10000,
    });
    assert.equal(result.error, undefined);
    assert.equal(result.status, 1, result.stderr || result.stdout);
    assert.match(result.stdout, /Failed to ensure trading tables/);
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});
