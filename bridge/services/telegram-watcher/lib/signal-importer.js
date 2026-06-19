const { execFile } = require("child_process");
const fs = require("fs");
const path = require("path");
const { canonicalWatchedChatId } = require("./telegram-utils");

const SIGNAL_IMPORTER_ENABLED = isSignalImporterEnabled(process.env.SIGNAL_IMPORTER_ENABLED);
const SIGNAL_IMPORTER_PYTHON = process.env.SIGNAL_IMPORTER_PYTHON || "python3";
const SIGNAL_IMPORTER_MODULE = process.env.SIGNAL_IMPORTER_MODULE || "";
const SIGNAL_IMPORTER_CWD = process.env.SIGNAL_IMPORTER_CWD || "";
const SIGNAL_IMPORTER_REMOTE_HOST = process.env.SIGNAL_IMPORTER_REMOTE_HOST || "";
const SIGNAL_IMPORTER_REMOTE_CWD = process.env.SIGNAL_IMPORTER_REMOTE_CWD || SIGNAL_IMPORTER_CWD;
const SIGNAL_IMPORTER_SSH = process.env.SIGNAL_IMPORTER_SSH || "ssh";
const SIGNAL_IMPORTER_SSH_IPV6 = isOptionalFlagEnabled(process.env.SIGNAL_IMPORTER_SSH_IPV6);
const SIGNAL_IMPORTER_SSH_CONNECT_TIMEOUT = parsePositiveInteger(
  process.env.SIGNAL_IMPORTER_SSH_CONNECT_TIMEOUT,
  10
);
const SIGNAL_IMPORTER_TIMEOUT_MS = parsePositiveInteger(process.env.SIGNAL_IMPORTER_TIMEOUT_MS, 10000);
const SIGNAL_IMPORTER_MAX_IN_FLIGHT = parsePositiveInteger(process.env.SIGNAL_IMPORTER_MAX_IN_FLIGHT, 4);
const SIGNAL_IMPORTER_DEBUG = isOptionalFlagEnabled(process.env.SIGNAL_IMPORTER_DEBUG);
const SIGNAL_STORE_URL = process.env.HERMES_SIGNAL_STORE_URL || process.env.SIGNAL_STORE_URL || "";
const SIGNAL_PAIR_WHITELIST = process.env.SIGNAL_PAIR_WHITELIST || "";
const IMPORTER_AUDIT_LOG = process.env.IMPORTER_AUDIT_LOG || "./importer-audit.jsonl";
const WATCHER_ALERT_BOT_TOKEN = process.env.WATCHER_ALERT_BOT_TOKEN || "";
const WATCHER_ALERT_CHAT_ID = process.env.WATCHER_ALERT_CHAT_ID || "";

const signalImporterRecentKeys = [];
const signalImporterRecentSet = new Set();
let signalImporterInFlight = 0;

function buildSignalImporterPayload(entry) {
  const media = entry.media || false;
  let normalizedMedia = false;

  if (media) {
    normalizedMedia = {
      type: media.type || "",
      filename: media.filename || "",
      path: media.path || "",
      mimeType: media.mimeType || "",
    };
  }

  return {
    source: "telegram",
    chatId: entry.chatId || "",
    chatTitle: entry.chatTitle || "",
    id: entry.id || 0,
    sender: entry.sender || "",
    text: entry.text || "",
    date: entry.date || "",
    media: normalizedMedia,
  };
}

function isSignalImporterEnabled(rawValue) {
  if (!rawValue) {
    return false;
  }

  const value = String(rawValue).trim().toLowerCase();
  return /^(1|true|on|yes)$/.test(value);
}

function isOptionalFlagEnabled(rawValue) {
  if (!rawValue) {
    return false;
  }

  const value = String(rawValue).trim().toLowerCase();
  return /^(1|true|on|yes)$/.test(value);
}

function parsePositiveInteger(rawValue, fallback) {
  const value = Number.parseInt(String(rawValue || ""), 10);
  if (!Number.isFinite(value)) {
    return fallback;
  }
  if (value <= 0) {
    return fallback;
  }
  return value;
}

function signalImporterKey(entry) {
  const chatId = canonicalWatchedChatId(entry.chatId || "");
  const messageId = entry.id || "";
  if (!chatId || !messageId) {
    return "";
  }
  return `${chatId}:${messageId}`;
}

function rememberSignalImporterKey(key) {
  if (!key) {
    return;
  }

  signalImporterRecentSet.add(key);
  signalImporterRecentKeys.push(key);
  while (signalImporterRecentKeys.length > 1000) {
    const oldestKey = signalImporterRecentKeys.shift();
    if (oldestKey) {
      signalImporterRecentSet.delete(oldestKey);
    }
  }
}

function finishSignalImporter() {
  if (signalImporterInFlight <= 0) {
    signalImporterInFlight = 0;
    return;
  }
  signalImporterInFlight -= 1;
}

function importerSummary(stdout) {
  try {
    const payload = JSON.parse(String(stdout || "{}"));
    return JSON.stringify({
      approved: Number(payload.approved || 0),
      needs_review: Number(payload.needs_review || 0),
      rejected: Number(payload.rejected || 0),
      total: Number(payload.total || 0),
    });
  } catch {
    return "ok";
  }
}

function importerResultState(err) {
  if (!err) {
    return "dispatched";
  }
  if (err.killed || err.code === "ETIMEDOUT" || err.signal === "SIGTERM") {
    return "timeout";
  }
  return "failed";
}

function auditPath() {
  if (path.isAbsolute(IMPORTER_AUDIT_LOG)) {
    return IMPORTER_AUDIT_LOG;
  }
  return path.resolve(process.cwd(), IMPORTER_AUDIT_LOG);
}

function appendImporterAudit(record) {
  const targetPath = auditPath();
  const parentDir = path.dirname(targetPath);
  try {
    fs.mkdirSync(parentDir, { recursive: true });
    fs.appendFileSync(targetPath, `${JSON.stringify(record)}\n`, { encoding: "utf8" });
  } catch (err) {
    console.error(`[signal-importer] audit write failed: ${allowlistedLogToken(err.code || err.name, "error")}`);
  }
}

function buildImporterAuditRecord(state, entry, err, stdout) {
  const record = {
    ts: new Date().toISOString(),
    state,
    chatId: entry.chatId || "",
    messageId: entry.id || 0,
    importKey: signalImporterKey(entry),
  };

  if (state === "dispatched") {
    record.summary = importerSummary(stdout);
  }
  if (err) {
    record.exitCode = allowlistedLogToken(err.code || err.name, "error");
    record.signal = allowlistedLogToken(err.signal || "", "");
  }

  return record;
}

function alertTextForImporterResult(state, record) {
  return [
    `[signal-importer] ${state}`,
    `chatId=${record.chatId || "unknown"}`,
    `messageId=${record.messageId || "unknown"}`,
    record.exitCode ? `exitCode=${record.exitCode}` : "",
    record.signal ? `signal=${record.signal}` : "",
  ].filter(Boolean).join(" ");
}

async function sendImporterAlert(state, record) {
  if (state === "dispatched") {
    return;
  }

  const text = alertTextForImporterResult(state, record);
  if (!WATCHER_ALERT_BOT_TOKEN || !WATCHER_ALERT_CHAT_ID) {
    console.error(`[signal-importer] alert skipped: missing Telegram alert configuration; ${text}`);
    return;
  }
  if (typeof fetch !== "function") {
    console.error(`[signal-importer] alert skipped: fetch unavailable; ${text}`);
    return;
  }

  try {
    const response = await fetch(`https://api.telegram.org/bot${WATCHER_ALERT_BOT_TOKEN}/sendMessage`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        chat_id: WATCHER_ALERT_CHAT_ID,
        text,
      }),
    });
    if (!response.ok) {
      console.error(`[signal-importer] alert failed: http_${response.status}`);
    }
  } catch (err) {
    console.error(`[signal-importer] alert failed: ${allowlistedLogToken(err.code || err.name, "error")}`);
  }
}

function recordImporterResult(state, entry, err, stdout) {
  const record = buildImporterAuditRecord(state, entry, err, stdout);
  appendImporterAudit(record);
  sendImporterAlert(state, record);
  return record;
}

function debugSignalImporterLog(label, value) {
  if (!SIGNAL_IMPORTER_DEBUG) {
    return;
  }
  console.log(`[signal-importer] ${allowlistedLogToken(label, "debug")}: ${safeDebugState(value)}`);
}

function safeDebugState(value) {
  if (!value) {
    return "empty";
  }
  return "present";
}

function allowlistedLogToken(value, fallback) {
  const token = String(value || "");
  if (/^[A-Za-z0-9_.-]{1,80}$/.test(token)) {
    return token;
  }
  return fallback;
}

function buildImporterArgs() {
  const args = [
    "-m",
    SIGNAL_IMPORTER_MODULE,
    "--stdin",
    "--store-url",
    SIGNAL_STORE_URL,
  ];

  if (SIGNAL_PAIR_WHITELIST) {
    args.push("--pair-whitelist", SIGNAL_PAIR_WHITELIST);
  }

  return args;
}

function shellQuote(value) {
  const text = String(value || "");
  return `'${text.replace(/'/g, "'\\''")}'`;
}

function buildRemoteCommand(args, payload) {
  const command = [shellQuote(SIGNAL_IMPORTER_PYTHON)];
  for (const arg of args) {
    command.push(shellQuote(arg));
  }

  let commandText = command.join(" ");
  if (payload) {
    const encodedPayload = Buffer.from(JSON.stringify(payload), "utf8").toString("base64");
    commandText = `printf %s ${shellQuote(encodedPayload)} | base64 -d | ${commandText}`;
  }

  if (!SIGNAL_IMPORTER_REMOTE_CWD) {
    return commandText;
  }

  return `cd ${shellQuote(SIGNAL_IMPORTER_REMOTE_CWD)} && ${commandText}`;
}

function buildImporterInvocation(payload = false) {
  const args = buildImporterArgs();
  const options = {
    timeout: SIGNAL_IMPORTER_TIMEOUT_MS,
    env: process.env,
  };

  if (SIGNAL_IMPORTER_REMOTE_HOST) {
    const sshArgs = [
      "-o",
      "BatchMode=yes",
      "-o",
      `ConnectTimeout=${SIGNAL_IMPORTER_SSH_CONNECT_TIMEOUT}`,
    ];
    if (SIGNAL_IMPORTER_SSH_IPV6) {
      sshArgs.unshift("-6");
    }
    sshArgs.push(SIGNAL_IMPORTER_REMOTE_HOST);
    sshArgs.push(buildRemoteCommand(args, payload));
    return {
      bin: SIGNAL_IMPORTER_SSH,
      args: sshArgs,
      options,
      usesStdin: false,
    };
  }

  if (SIGNAL_IMPORTER_CWD) {
    options.cwd = SIGNAL_IMPORTER_CWD;
  }

  return {
    bin: SIGNAL_IMPORTER_PYTHON,
    args,
    options,
    usesStdin: true,
  };
}

function importSignalToFreqtrade(entry) {
  if (!SIGNAL_IMPORTER_ENABLED) {
    return;
  }

  if (!SIGNAL_STORE_URL || !SIGNAL_IMPORTER_MODULE) {
    return;
  }

  const importKey = signalImporterKey(entry);
  if (importKey && signalImporterRecentSet.has(importKey)) {
    return;
  }

  if (signalImporterInFlight >= SIGNAL_IMPORTER_MAX_IN_FLIGHT) {
    console.log("[signal-importer] skipped: max in-flight reached");
    return;
  }

  const payload = buildSignalImporterPayload(entry);
  const invocation = buildImporterInvocation(payload);
  signalImporterInFlight += 1;

  let child;
  try {
    child = execFile(invocation.bin, invocation.args, invocation.options, (err, stdout, stderr) => {
      finishSignalImporter();
      const state = importerResultState(err);
      recordImporterResult(state, entry, err, stdout);
      if (err) {
        const code = allowlistedLogToken(err.code || err.name, "error");
        console.log(`[signal-importer] ${state}: ${code}`);
        debugSignalImporterLog("stderr", stderr);
        return;
      }

      rememberSignalImporterKey(importKey);
      if (stdout) {
        console.log(`[signal-importer] imported: ${importerSummary(stdout)}`);
      }
      if (stderr) {
        debugSignalImporterLog("stderr", stderr);
      }
    });
  } catch (err) {
    finishSignalImporter();
    if (err) {
      const code = allowlistedLogToken(err.code || err.name, "error");
      console.log(`[signal-importer] failed: ${code}`);
    }
    return;
  }

  if (!invocation.usesStdin) {
    return;
  }

  if (!child.stdin) {
    finishSignalImporter();
    return;
  }

  child.stdin.on("error", (err) => {
    const code = allowlistedLogToken(err.code || err.name, "stdin_error");
    console.log(`[signal-importer] stdin failed: ${code}`);
  });

  child.stdin.write(JSON.stringify(payload));
  child.stdin.end();
}

module.exports = {
  importSignalToFreqtrade,
  __test: {
    buildImporterInvocation,
    getSignalImporterInFlight() {
      return signalImporterInFlight;
    },
  },
};
