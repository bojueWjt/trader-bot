const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");

test("real server exits before listening when trading database initialization fails", () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "watcher-init-failure-"));
  const mediaDirectory = path.join(directory, "media");
  const serverPath = path.join(__dirname, "..", "server.js");
  try {
    const result = spawnSync(process.execPath, [serverPath], {
      cwd: directory,
      env: {
        PATH: process.env.PATH,
        TRADER_TRADING_DB_PATH: directory,
        WATCHER_MEDIA_DIR: mediaDirectory,
        WATCHER_HOST: "127.0.0.1",
      },
      encoding: "utf8",
      timeout: 10000,
    });
    assert.equal(result.error, undefined);
    assert.equal(result.status, 1, result.stderr || result.stdout);
    assert.equal(result.signal, null);
    assert.match(result.stdout, /Failed to ensure trading tables/);
    assert.doesNotMatch(result.stdout + result.stderr, /Web UI listening/);
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});
