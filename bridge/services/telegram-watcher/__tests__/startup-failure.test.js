const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");

test("real server exits before listening when trading database initialization fails", () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "watcher-init-failure-"));
  const mediaDirectory = path.join(directory, "media");
  const serverPath = path.join(__dirname, "..", "server.js");
  const tokens = new Set();
  while (tokens.size < 3) {
    tokens.add(crypto.randomBytes(24).toString("hex"));
  }
  const [gatewayToken, snapshotToken, browserProxyToken] = tokens;
  try {
    assert.equal(fs.existsSync(path.join(path.dirname(serverPath), "config.json")), false, "startup test must not read local Telegram config");
    const result = spawnSync(process.execPath, [serverPath], {
      cwd: directory,
      env: {
        PATH: process.env.PATH,
        TRADER_TRADING_DB_PATH: directory,
        WATCHER_MEDIA_DIR: mediaDirectory,
        WATCHER_HOST: "127.0.0.1",
        WATCHER_GATEWAY_TOKEN: gatewayToken,
        WATCHER_SNAPSHOT_TOKEN: snapshotToken,
        WATCHER_BROWSER_PROXY_TOKEN: browserProxyToken,
      },
      encoding: "utf8",
      timeout: 10000,
    });
    assert.equal(result.error, undefined);
    assert.equal(result.status, 1, result.stderr || result.stdout);
    assert.equal(result.signal, null);
    assert.match(result.stdout + result.stderr, /Failed to ensure trading tables/);
    assert.match(result.stdout + result.stderr, /Exiting with code 1: database_initialization_failed/);
    assert.doesNotMatch(result.stdout + result.stderr, /Web UI listening/);
    for (const token of tokens) {
      assert.equal((result.stdout + result.stderr).includes(token), false);
    }
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});
