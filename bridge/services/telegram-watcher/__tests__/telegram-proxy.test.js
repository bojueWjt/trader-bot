const assert = require("node:assert/strict");
const test = require("node:test");

const { buildTelegramClientOptions } = require("../lib/telegram-proxy");

test("telegram proxy defaults to local SOCKS5", () => {
  const options = buildTelegramClientOptions({});

  assert.deepEqual(options, {
    connectionRetries: 5,
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
    useWSS: true,
  });
});

test("telegram WSS transport stays off when unset", () => {
  const options = buildTelegramClientOptions({
    TELEGRAM_PROXY_ENABLED: "0",
  });

  assert.equal(options.useWSS, undefined);
});
