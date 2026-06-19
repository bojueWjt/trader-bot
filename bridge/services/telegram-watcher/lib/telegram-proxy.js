const { isEnabledByDefault, isExplicitlyEnabled } = require("./env-flags");

function parsePositiveInteger(rawValue, fallback) {
  const value = Number.parseInt(rawValue, 10);
  if (!Number.isFinite(value)) {
    return fallback;
  }
  if (value <= 0) {
    return fallback;
  }
  return value;
}

function buildTelegramClientOptions(env = process.env) {
  const options = {
    connectionRetries: parsePositiveInteger(env.TELEGRAM_CONNECTION_RETRIES, 5),
  };

  // WSS rides TLS on 443; needed where plain MTProto TCP gets DPI-killed
  // (connect succeeds, then "connection closed while receiving data" loops)
  if (isExplicitlyEnabled(env.TELEGRAM_USE_WSS)) {
    options.useWSS = true;
  }

  if (!isEnabledByDefault(env.TELEGRAM_PROXY_ENABLED)) {
    return options;
  }

  options.proxy = {
    ip: env.TELEGRAM_PROXY_HOST || "127.0.0.1",
    port: parsePositiveInteger(env.TELEGRAM_PROXY_PORT, 7897),
    socksType: parsePositiveInteger(env.TELEGRAM_PROXY_SOCKS_TYPE, 5),
    timeout: parsePositiveInteger(env.TELEGRAM_PROXY_TIMEOUT, 10),
  };
  return options;
}

function describeTelegramProxy(options) {
  const transport = options.useWSS ? " (WSS transport)" : "";
  if (!options.proxy) {
    return `disabled${transport}`;
  }

  const { ip, port, socksType } = options.proxy;
  return `SOCKS${socksType} ${ip}:${port}${transport}`;
}

module.exports = {
  buildTelegramClientOptions,
  describeTelegramProxy,
};
