const assert = require("node:assert/strict");
const test = require("node:test");

const priceMonitorPath = require.resolve("../price-monitor");
const childProcess = require("child_process");
const Module = require("module");
const originalExecFile = childProcess.execFile;
const originalLoad = Module._load;

function createFakeDatabase() {
  return function FakeDatabase() {
    return {
      close() {},
      exec() {},
      pragma() {},
      prepare() {
        return {
          all() {
            return [];
          },
          get() {
            return false;
          },
          run() {
            return { changes: 0 };
          },
        };
      },
    };
  };
}

function loadPriceMonitor(envOverrides, execFileImpl) {
  delete require.cache[priceMonitorPath];

  const originalEnv = process.env;
  process.env = {
    ...originalEnv,
    ...envOverrides,
  };

  childProcess.execFile = execFileImpl;
  Module._load = function load(request, parent, isMain) {
    if (request === "better-sqlite3") {
      return createFakeDatabase();
    }
    return originalLoad.call(this, request, parent, isMain);
  };
  const priceMonitor = require("../price-monitor");

  function cleanup() {
    priceMonitor.stop();
    childProcess.execFile = originalExecFile;
    Module._load = originalLoad;
    process.env = originalEnv;
    delete require.cache[priceMonitorPath];
  }

  return { cleanup, priceMonitor };
}

test("disabled price monitor start leaves interval idle", () => {
  const { cleanup, priceMonitor } = loadPriceMonitor(
    { PRICE_MONITOR_ENABLED: "0" },
    () => {
      throw new Error("execFile should stay idle");
    }
  );

  try {
    priceMonitor.start();

    const status = priceMonitor.getStatus();
    assert.equal(status.running, false);
    assert.equal(status.enabled, false);
  } finally {
    cleanup();
  }
});

test("price alert never invokes a direct execution process", () => {
  const { cleanup, priceMonitor } = loadPriceMonitor(
    { PRICE_MONITOR_ENABLED: "1" },
    () => {
      throw new Error("execFile should stay idle");
    }
  );

  try {
    const triggered = priceMonitor.__test.triggerTrader(
      {
        id: 123,
        symbol: "BTCUSDT",
        target_price: 100,
        alert_type: "tp",
        direction: "above",
        order_id: 456,
      },
      101
    );

    assert.equal(triggered, false);
  } finally {
    cleanup();
  }
});
