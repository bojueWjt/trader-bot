const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const importerPath = require.resolve("../lib/signal-importer");
const childProcess = require("child_process");
const originalExecFile = childProcess.execFile;

function loadSignalImporter(envOverrides, execFileImpl) {
  delete require.cache[importerPath];

  const originalEnv = process.env;
  process.env = {
    ...originalEnv,
    ...envOverrides,
  };

  childProcess.execFile = execFileImpl;
  const importer = require("../lib/signal-importer");

  function cleanup() {
    childProcess.execFile = originalExecFile;
    process.env = originalEnv;
    delete require.cache[importerPath];
  }

  return { importer, cleanup };
}

test("disabled importer returns without spawning python", () => {
  const { importer, cleanup } = loadSignalImporter(
    {
      SIGNAL_IMPORTER_ENABLED: "0",
      HERMES_SIGNAL_STORE_URL: "store-for-test",
    },
    () => {
      throw new Error("execFile should stay idle");
    }
  );

  try {
    importer.importSignalToFreqtrade({ chatId: "chat-a", id: 1, text: "BTC long" });

    assert.equal(importer.__test.getSignalImporterInFlight(), 0);
  } finally {
    cleanup();
  }
});

test("importer defaults disabled and writes no audit when flag is omitted", () => {
  let execCalls = 0;
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "signal-importer-audit-"));
  const auditPath = path.join(tmpDir, "audit.jsonl");
  const fakeStdin = new EventEmitter();
  fakeStdin.write = () => {};
  fakeStdin.end = () => {};

  const { importer, cleanup } = loadSignalImporter(
    {
      HERMES_SIGNAL_STORE_URL: "store-for-test",
      IMPORTER_AUDIT_LOG: auditPath,
    },
    () => {
      execCalls += 1;
      return { stdin: fakeStdin };
    }
  );

  try {
    importer.importSignalToFreqtrade({ chatId: "chat-a", id: 10, text: "BTC long" });

    assert.equal(execCalls, 0);
    assert.equal(importer.__test.getSignalImporterInFlight(), 0);
    assert.equal(fs.existsSync(auditPath), false);
  } finally {
    cleanup();
  }
});

test("importer failure callback releases in-flight slot and keeps caller unblocked", async () => {
  let execCalls = 0;
  const fakeStdin = new EventEmitter();
  fakeStdin.write = () => {};
  fakeStdin.end = () => {};
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "signal-importer-audit-"));

  const { importer, cleanup } = loadSignalImporter(
    {
      SIGNAL_IMPORTER_ENABLED: "1",
      HERMES_SIGNAL_STORE_URL: "store-for-test",
      SIGNAL_IMPORTER_MODULE: "test.signal_importer",
      SIGNAL_IMPORTER_MAX_IN_FLIGHT: "1",
      IMPORTER_AUDIT_LOG: path.join(tmpDir, "audit.jsonl"),
    },
    (bin, args, options, cb) => {
      execCalls += 1;
      setImmediate(() => {
        const err = new Error("boom");
        err.code = "TEST_FAILURE";
        cb(err, "", "sensitive details stay behind debug flag");
      });
      return { stdin: fakeStdin };
    }
  );

  const originalLog = console.log;
  const logs = [];
  console.log = (line) => {
    logs.push(String(line));
  };

  try {
    importer.importSignalToFreqtrade({ chatId: "chat-a", id: 2, text: "ETH long" });

    assert.equal(execCalls, 1);
    assert.equal(importer.__test.getSignalImporterInFlight(), 1);

    await new Promise((resolve) => {
      setImmediate(resolve);
    });

    assert.equal(importer.__test.getSignalImporterInFlight(), 0);
    assert.deepEqual(logs, ["[signal-importer] failed: TEST_FAILURE"]);
  } finally {
    console.log = originalLog;
    cleanup();
  }
});

test("remote importer embeds payload in command because ssh stdin can be closed", () => {
  const { importer, cleanup } = loadSignalImporter(
    {
      SIGNAL_IMPORTER_ENABLED: "1",
      HERMES_SIGNAL_STORE_URL: "sqlite:////root/freqtrade/user_data/signal_strategy.sqlite3",
      SIGNAL_IMPORTER_MODULE: "test.signal_importer",
      SIGNAL_IMPORTER_REMOTE_HOST: "root@example.test",
      SIGNAL_IMPORTER_REMOTE_CWD: "/root/freqtrade",
      SIGNAL_IMPORTER_SSH_IPV6: "1",
      SIGNAL_PAIR_WHITELIST: "BTC/USDT:USDT",
    },
    () => {
      throw new Error("execFile should stay idle");
    }
  );

  try {
    const invocation = importer.__test.buildImporterInvocation({
      source: "telegram",
      chatId: "chat-a",
      id: 3,
      text: "BTC long",
    });
    const remoteCommand = invocation.args[invocation.args.length - 1];

    assert.equal(invocation.bin, "ssh");
    assert.equal(invocation.usesStdin, false);
    assert.equal(invocation.args[0], "-6");
    assert.ok(invocation.args.includes("BatchMode=yes"));
    assert.ok(invocation.args.includes("root@example.test"));
    assert.ok(remoteCommand.startsWith("cd '/root/freqtrade' && printf %s "));
    assert.ok(remoteCommand.includes("| base64 -d | 'python3' '-m'"));
    assert.ok(remoteCommand.includes("'test.signal_importer'"));
    assert.ok(remoteCommand.includes("'sqlite:////root/freqtrade/user_data/signal_strategy.sqlite3'"));
    assert.equal(remoteCommand.includes("'--approve-parsed'"), false);
    assert.equal(remoteCommand.includes("'--refresh-window'"), false);
    assert.ok(remoteCommand.includes("'BTC/USDT:USDT'"));
  } finally {
    cleanup();
  }
});

test("local importer keeps stdin payload path", () => {
  const { importer, cleanup } = loadSignalImporter(
    {
      SIGNAL_IMPORTER_ENABLED: "1",
      HERMES_SIGNAL_STORE_URL: "sqlite:///local.sqlite3",
      SIGNAL_IMPORTER_MODULE: "test.signal_importer",
      SIGNAL_IMPORTER_CWD: "/tmp/freqtrade",
    },
    () => {
      throw new Error("execFile should stay idle");
    }
  );

  try {
    const invocation = importer.__test.buildImporterInvocation({
      source: "telegram",
      chatId: "chat-a",
      id: 4,
      text: "ETH long",
    });

    assert.equal(invocation.bin, "python3");
    assert.equal(invocation.usesStdin, true);
    assert.equal(invocation.options.cwd, "/tmp/freqtrade");
    assert.ok(invocation.args.includes("--stdin"));
    assert.equal(invocation.args.includes("--approve-parsed"), false);
    assert.equal(invocation.args.includes("--refresh-window"), false);
  } finally {
    cleanup();
  }
});

test.describe("importer dispatch audit and alert states", () => {
  function makeEntry(id) {
    return {
      chatId: "chat-a",
      chatTitle: "Signals",
      id,
      sender: "tester",
      text: "BTC 买入策略\n入场点：65000\n止损位：64000\n目标价：67000",
      date: "2026-06-10T00:00:00.000Z",
    };
  }

  function makeChild() {
    const fakeStdin = new EventEmitter();
    fakeStdin.write = () => {};
    fakeStdin.end = () => {};
    return { stdin: fakeStdin };
  }

  function readAudit(auditPath) {
    return fs.readFileSync(auditPath, "utf8").trim().split("\n").map((line) => JSON.parse(line));
  }

  async function waitForAsyncCallbacks() {
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
  }

  test("successful importer callback writes dispatched audit state", async () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "signal-importer-audit-"));
    const auditPath = path.join(tmpDir, "audit.jsonl");
    const originalFetch = global.fetch;
    const fetchCalls = [];
    global.fetch = async (...args) => {
      fetchCalls.push(args);
      return { ok: true, status: 200, text: async () => "ok" };
    };

    const { importer, cleanup } = loadSignalImporter(
      {
        SIGNAL_IMPORTER_ENABLED: "1",
        HERMES_SIGNAL_STORE_URL: "store-for-test",
        SIGNAL_IMPORTER_MODULE: "test.signal_importer",
        IMPORTER_AUDIT_LOG: auditPath,
        WATCHER_ALERT_BOT_TOKEN: "alert-token",
        WATCHER_ALERT_CHAT_ID: "chat-1",
      },
      (bin, args, options, cb) => {
        setImmediate(() => cb(null, JSON.stringify({ approved: 1, needs_review: 0, rejected: 0, total: 1 }), ""));
        return makeChild();
      }
    );

    try {
      importer.importSignalToFreqtrade(makeEntry(101));
      await waitForAsyncCallbacks();

      const audit = readAudit(auditPath);
      assert.equal(audit.length, 1);
      assert.equal(audit[0].state, "dispatched");
      assert.equal(audit[0].messageId, 101);
      assert.equal(audit[0].chatId, "chat-a");
      assert.deepEqual(fetchCalls, []);
    } finally {
      global.fetch = originalFetch;
      cleanup();
    }
  });

  test("non-zero importer callback writes failed audit state and sends alert", async () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "signal-importer-audit-"));
    const auditPath = path.join(tmpDir, "audit.jsonl");
    const originalFetch = global.fetch;
    const fetchCalls = [];
    global.fetch = async (...args) => {
      fetchCalls.push(args);
      return { ok: true, status: 200, text: async () => "ok" };
    };

    const { importer, cleanup } = loadSignalImporter(
      {
        SIGNAL_IMPORTER_ENABLED: "1",
        HERMES_SIGNAL_STORE_URL: "store-for-test",
        SIGNAL_IMPORTER_MODULE: "test.signal_importer",
        IMPORTER_AUDIT_LOG: auditPath,
        WATCHER_ALERT_BOT_TOKEN: "alert-token",
        WATCHER_ALERT_CHAT_ID: "chat-1",
      },
      (bin, args, options, cb) => {
        setImmediate(() => {
          const err = new Error("exit 2");
          err.code = 2;
          cb(err, "", "bad import");
        });
        return makeChild();
      }
    );

    try {
      importer.importSignalToFreqtrade(makeEntry(102));
      await waitForAsyncCallbacks();

      const audit = readAudit(auditPath);
      assert.equal(audit.length, 1);
      assert.equal(audit[0].state, "failed");
      assert.equal(audit[0].exitCode, "2");
      assert.equal(fetchCalls.length, 1);
      assert.equal(fetchCalls[0][0], "https://api.telegram.org/botalert-token/sendMessage");
      const body = JSON.parse(fetchCalls[0][1].body);
      assert.equal(body.chat_id, "chat-1");
      assert.match(body.text, /failed/);
      assert.match(body.text, /102/);
    } finally {
      global.fetch = originalFetch;
      cleanup();
    }
  });

  test("timed-out importer callback writes timeout audit state and sends alert", async () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "signal-importer-audit-"));
    const auditPath = path.join(tmpDir, "audit.jsonl");
    const originalFetch = global.fetch;
    const fetchCalls = [];
    global.fetch = async (...args) => {
      fetchCalls.push(args);
      return { ok: true, status: 200, text: async () => "ok" };
    };

    const { importer, cleanup } = loadSignalImporter(
      {
        SIGNAL_IMPORTER_ENABLED: "1",
        HERMES_SIGNAL_STORE_URL: "store-for-test",
        SIGNAL_IMPORTER_MODULE: "test.signal_importer",
        IMPORTER_AUDIT_LOG: auditPath,
        WATCHER_ALERT_BOT_TOKEN: "alert-token",
        WATCHER_ALERT_CHAT_ID: "chat-1",
      },
      (bin, args, options, cb) => {
        setImmediate(() => {
          const err = new Error("timed out");
          err.killed = true;
          err.signal = "SIGTERM";
          cb(err, "", "timeout");
        });
        return makeChild();
      }
    );

    try {
      importer.importSignalToFreqtrade(makeEntry(103));
      await waitForAsyncCallbacks();

      const audit = readAudit(auditPath);
      assert.equal(audit.length, 1);
      assert.equal(audit[0].state, "timeout");
      assert.equal(audit[0].signal, "SIGTERM");
      assert.equal(fetchCalls.length, 1);
      const body = JSON.parse(fetchCalls[0][1].body);
      assert.equal(body.chat_id, "chat-1");
      assert.match(body.text, /timeout/);
      assert.match(body.text, /103/);
    } finally {
      global.fetch = originalFetch;
      cleanup();
    }
  });
});
