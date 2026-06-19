const SENSITIVE_KEY_PATTERN = /(api[_-]?key|api[_-]?secret|apisecret|apiHash|token|session|password)/i;
const SENSITIVE_TEXT_PATTERN = /(api[_-]?key|api[_-]?secret|apiSecret|apiHash|[A-Za-z0-9_-]*token|[A-Za-z0-9_-]*session|[A-Za-z0-9_-]*password)(\s*[:=]\s*)(["']?)([^"',\s}\]]+)/gi;
const IPV4_PATTERN = /\b(?:\d{1,3}\.){3}\d{1,3}\b/g;

function isSensitiveKey(key) {
  return SENSITIVE_KEY_PATTERN.test(String(key || ""));
}

function maskSecret(value) {
  if (!value) {
    return "****";
  }

  const text = String(value);
  if (text.length <= 8) {
    return "****";
  }

  return `${text.substring(0, 4)}...${text.substring(text.length - 4)}`;
}

function redactSensitiveText(value) {
  const text = String(value || "");
  if (!text) {
    return "";
  }

  return text
    .replace(SENSITIVE_TEXT_PATTERN, (match, key, separator, quote) => {
      return `${key}${separator}${quote}****`;
    })
    .replace(IPV4_PATTERN, "[ip]");
}

function safeErrorMessage(err) {
  if (!err) {
    return "error";
  }

  if (err.message) {
    return redactSensitiveText(err.message);
  }

  return redactSensitiveText(err);
}

function safeErrorStack(err) {
  if (!err) {
    return "";
  }

  if (!err.stack) {
    return "";
  }

  return redactSensitiveText(err.stack);
}

function safeOutputSummary(value) {
  const text = String(value || "");
  if (!text) {
    return "empty";
  }

  return `present (${text.length} bytes)`;
}

function allowlistedLogToken(value, fallback) {
  const token = String(value || "");
  if (/^[A-Za-z0-9_.-]{1,80}$/.test(token)) {
    return token;
  }
  return fallback;
}

function sanitizeForResponse(value) {
  if (Array.isArray(value)) {
    return value.map((item) => sanitizeForResponse(item));
  }

  if (!value || typeof value !== "object") {
    return value;
  }

  const output = {};
  for (const key of Object.keys(value)) {
    const item = value[key];
    if (isSensitiveKey(key)) {
      output[key] = maskSecret(item);
      continue;
    }
    output[key] = sanitizeForResponse(item);
  }
  return output;
}

module.exports = {
  allowlistedLogToken,
  maskSecret,
  redactSensitiveText,
  safeErrorMessage,
  safeErrorStack,
  safeOutputSummary,
  sanitizeForResponse,
};
