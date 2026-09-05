const assert = require("node:assert/strict");
const test = require("node:test");

const { buildTelegramClientOptions, installTelegramReconnectBackoff } = require("../lib/telegram-proxy");

test("telegram proxy defaults to local SOCKS5", () => {
  const options = buildTelegramClientOptions({});

  assert.deepEqual(options, {
    connectionRetries: 5,
    reconnectRetries: 5,
    retryDelay: 10000,
    proxy: {
      ip: "127.0.0.1",
      port: 7897,
      socksType: 5,
      timeout: 10,
    },
  });
});

test("telegram proxy can be disabled by env", () => {
  const options = buildTelegramClientOptions({
    TELEGRAM_PROXY_ENABLED: "0",
  });

  assert.deepEqual(options, {
    connectionRetries: 5,
    reconnectRetries: 5,
    retryDelay: 10000,
  });
});

test("telegram proxy accepts explicit host port and timeout", () => {
  const options = buildTelegramClientOptions({
    TELEGRAM_PROXY_HOST: "127.0.0.1",
    TELEGRAM_PROXY_PORT: "7898",
    TELEGRAM_PROXY_SOCKS_TYPE: "5",
    TELEGRAM_PROXY_TIMEOUT: "20",
  });

  assert.deepEqual(options, {
    connectionRetries: 5,
    reconnectRetries: 5,
    retryDelay: 10000,
    proxy: {
      ip: "127.0.0.1",
      port: 7898,
      socksType: 5,
      timeout: 20,
    },
  });
});

test("telegram WSS transport is opt-in via env", () => {
  const options = buildTelegramClientOptions({
    TELEGRAM_PROXY_ENABLED: "0",
    TELEGRAM_USE_WSS: "1",
  });

  assert.deepEqual(options, {
    connectionRetries: 5,
    reconnectRetries: 5,
    retryDelay: 10000,
    useWSS: true,
  });
});

test("telegram WSS transport stays off when unset", () => {
  const options = buildTelegramClientOptions({
    TELEGRAM_PROXY_ENABLED: "0",
  });

  assert.equal(options.useWSS, undefined);
});

test("retry policy stays finite with invalid environment settings", () => {
  const options = buildTelegramClientOptions({
    TELEGRAM_CONNECTION_RETRIES: "Infinity",
    TELEGRAM_RECONNECT_RETRIES: "Infinity",
    TELEGRAM_RETRY_DELAY: "0",
  });

  assert.equal(options.connectionRetries, 5);
  assert.equal(options.reconnectRetries, 5);
  assert.equal(options.retryDelay, 10000);
});

test("actual GramJS reconnect entry is single-flight and its log cadence is throttled", async (context) => {
  const { MTProtoSender } = require("telegram/network/MTProtoSender");
  assert.equal(typeof MTProtoSender.prototype._reconnect, "function");
  context.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 0 });
  const startedAt = [];
  let connections = 0;
  class Sender {
    constructor() {
      this._userConnected = true;
      this._currentRetries = 0;
      this._log = { info() { startedAt.push(Date.now()); } };
    }

    async _reconnect() {
      connections += 1;
      this.isReconnecting = false;
    }
  }
  Sender.prototype.reconnect = MTProtoSender.prototype.reconnect;
  installTelegramReconnectBackoff(Sender);
  installTelegramReconnectBackoff(Sender);
  const sender = new Sender();
  async function tick(milliseconds) {
    context.mock.timers.tick(milliseconds);
    for (let round = 0; round < 10; round += 1) {
      await Promise.resolve();
    }
  }

  sender.reconnect();
  sender.reconnect();
  await tick(1000);
  assert.deepEqual(startedAt, [1000]);
  assert.equal(connections, 0);
  assert.equal(sender.isReconnecting, true);
  sender.reconnect();
  await tick(9999);
  assert.equal(connections, 0);
  await tick(1);
  assert.equal(connections, 1);
  sender.reconnect();
  await tick(1000);
  assert.deepEqual(startedAt, [1000, 12000]);
  sender.userDisconnected = true;
  await tick(10000);
  assert.equal(connections, 1);
});
