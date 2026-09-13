const express = require("express");
const path = require("path");
const fs = require("fs");
const { TelegramClient, Api } = require("telegram");
const { StringSession } = require("telegram/sessions");
const { NewMessage } = require("telegram/events");
const { MTProtoSender } = require("telegram/network/MTProtoSender");
const { createWatchedEntryHandler } = require("./lib/watched-entry-routing");
const {
  chatIdFromEntity,
  chatIdFromPeerId,
  chatIdMatchesWatchList,
  documentMimeType,
  mediaExtension,
  shortUpdateText,
  updateName,
  webpageValue,
} = require("./lib/telegram-utils");
const {
  buildTelegramClientOptions,
  describeTelegramProxy,
  installTelegramReconnectBackoff,
} = require("./lib/telegram-proxy");
const {
  ensureTelegramMessagesTable,
  registerTradingApi,
  saveTelegramMessage,
} = require("./lib/trading-api");
const {
  safeErrorMessage,
  safeErrorStack,
} = require("./lib/safe-log");

// --- 守护：捕获未处理异常，防止进程崩溃 ---
process.on("uncaughtException", (err) => {
  const stack = safeErrorStack(err);
  console.error(`[FATAL] uncaughtException: ${safeErrorMessage(err)}\n${stack}`);
});
process.on("unhandledRejection", (reason) => {
  console.error(`[FATAL] unhandledRejection: ${safeErrorMessage(reason)}`);
});

// 优雅关闭
process.on("SIGINT", gracefulShutdown);
process.on("SIGTERM", gracefulShutdown);

function gracefulShutdown() {
  console.log("[watcher] Shutting down gracefully...");
  priceMonitor.stop();
  if (client) {
    client.disconnect().catch(() => {});
  }
  process.exit(0);
}

// --- Price Monitor ---
const priceMonitor = require("./price-monitor");

const app = express();

const CONFIG_PATH = path.join(__dirname, "config.json");
const MESSAGES_PATH = path.join(__dirname, "messages.json");
const MEDIA_DIR = process.env.WATCHER_MEDIA_DIR || path.join(__dirname, "media");
if (!fs.existsSync(MEDIA_DIR)) {
  fs.mkdirSync(MEDIA_DIR, { recursive: true });
}

app.use(express.json());
app.use(express.static(path.join(__dirname, "public")));
app.use("/media", express.static(MEDIA_DIR));

// --- State ---
let client = null;
let loginState = null; // { resolve, phase }
let qrLoginState = null; // { url, phase, done, error, passwordResolve, client }
let connected = false;
let watchedMessages = []; // recent messages ring buffer
const MAX_MESSAGES = 500;
const DISCONNECT_EXIT_MS = 120000;
const STALE_PROBE_MS = 20 * 60 * 1000;
const HEALTH_STALE_MS = 15 * 60 * 1000;
const POLL_MAX_AGE_MS = 30 * 60 * 1000;
const PROBE_TIMEOUT_MS = 10000;
const EXIT_ALERT_TIMEOUT_MS = 5000;
let lastUpdateAt = Date.now();
let disconnectedSince = false;
let hasConnected = false;
let watcherMonitoring = false;
let watcherExiting = false;
let probeInFlight = false;
let startingListener = false;
let pollTimer = false;

async function sendWatcherAlert(text) {
  console.log(`[watcher] ${text}`);
  const token = (process.env.WATCHER_ALERT_BOT_TOKEN || "").trim();
  const chatId = (process.env.WATCHER_ALERT_CHAT_ID || "").trim();
  if (!token || !chatId) {
    console.log("[watcher] Alert skipped: missing WATCHER_ALERT_BOT_TOKEN/WATCHER_ALERT_CHAT_ID");
    return;
  }

  try {
    const response = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chatId, text: `[watcher] ${text}` }),
      signal: AbortSignal.timeout(3000),
    });
    const result = await response.json();
    if (!response.ok || !result || result.ok !== true) {
      console.error("[watcher] Alert delivery rejected by Telegram");
    }
  } catch {
    console.error("[watcher] Alert delivery failed");
  }
}

function exitWatcher(reason, disconnected = false) {
  if (watcherExiting) {
    return;
  }
  watcherExiting = true;
  console.error(`[watcher] Exiting with code 1: ${reason}`);
  let exitRequested = false;
  const exitTimer = setTimeout(finishExit, EXIT_ALERT_TIMEOUT_MS);
  function finishExit() {
    if (exitRequested) {
      return;
    }
    exitRequested = true;
    clearTimeout(exitTimer);
    process.exit(1);
  }
  const alerts = [];
  if (disconnected) {
    alerts.push(sendWatcherAlert("Disconnected for at least 120 seconds"));
  }
  alerts.push(sendWatcherAlert(`Exiting with code 1: ${reason}`));
  Promise.allSettled(alerts).then(finishExit);
}

function isTelegramConnected() {
  if (!client || client.connected !== true || client._reconnecting) {
    return false;
  }
  const { _sender: sender } = client;
  if (sender && (sender.isReconnecting || sender.userDisconnected === true)) {
    return false;
  }
  return true;
}

function observeConnection() {
  connected = isTelegramConnected();
  if (!watcherMonitoring || watcherExiting) {
    return;
  }
  if (!connected) {
    if (disconnectedSince === false) {
      disconnectedSince = Date.now();
    }
    return;
  }
  if (disconnectedSince !== false) {
    disconnectedSince = false;
    if (hasConnected) {
      void sendWatcherAlert("Reconnected successfully");
    }
  }
  hasConnected = true;
}

async function checkWatcherHealth() {
  if (!watcherMonitoring || watcherExiting) {
    return;
  }
  observeConnection();
  if (disconnectedSince !== false && Date.now() - disconnectedSince >= DISCONNECT_EXIT_MS) {
    exitWatcher("continuous Telegram disconnect exceeded 120 seconds", true);
    return;
  }
  if (probeInFlight || Date.now() - lastUpdateAt <= STALE_PROBE_MS) {
    return;
  }
  probeInFlight = true;
  let probeTimer = false;
  const probeClient = client;
  try {
    const deadline = new Promise((resolve, reject) => {
      probeTimer = setTimeout(() => reject(new Error("Telegram probe timed out")), PROBE_TIMEOUT_MS);
    });
    await Promise.race([probeClient.getMessages("me", { limit: 1 }), deadline]);
    if (client === probeClient && watcherMonitoring && !watcherExiting) {
      lastUpdateAt = Date.now();
    }
  } catch (err) {
    if (watcherMonitoring && client === probeClient && Date.now() - lastUpdateAt > STALE_PROBE_MS) {
      exitWatcher(`no updates for over 20 minutes and getMessages probe failed: ${safeErrorMessage(err)}`);
    }
  } finally {
    clearTimeout(probeTimer);
    probeInFlight = false;
  }
}

setInterval(checkWatcherHealth, 5000);

// Load persisted messages on startup
try {
  const saved = JSON.parse(fs.readFileSync(MESSAGES_PATH, "utf-8"));
  if (Array.isArray(saved)) {
    watchedMessages = saved;
  }
  console.log(`[watcher] Loaded ${watchedMessages.length} messages from disk`);
} catch {}

// --- Config ---
function loadConfig() {
  try {
    return JSON.parse(fs.readFileSync(CONFIG_PATH, "utf-8"));
  } catch {
    return {};
  }
}

function saveConfig(cfg) {
  fs.writeFileSync(CONFIG_PATH, JSON.stringify(cfg, null, 2));
}

function pushMessage(msg) {
  watchedMessages.push(msg);
  if (watchedMessages.length > MAX_MESSAGES) {
    watchedMessages.shift();
  }
  // persist last N
  try {
    fs.writeFileSync(MESSAGES_PATH, JSON.stringify(watchedMessages, null, 2));
  } catch {}
}

async function loadMessageMedia(c, message) {
  if (!message.media) {
    return false;
  }

  const mediaType = message.media.className;
  if (mediaType === "MessageMediaPhoto" || mediaType === "MessageMediaDocument") {
    const ext = mediaType === "MessageMediaPhoto" ? "jpg" :
      mediaExtension(message.media, "bin");
    const filename = `${Date.now()}-${message.id}.${ext}`;
    const filepath = path.join(MEDIA_DIR, filename);
    const buffer = await c.downloadMedia(message.media, {});
    if (!buffer) {
      return false;
    }

    fs.writeFileSync(filepath, buffer);
    const mimeType = mediaType === "MessageMediaPhoto" ? "image/jpeg" :
      (documentMimeType(message.media) || "application/octet-stream");
    console.log(`[media] Saved -> ${filename} (${buffer.length} bytes)`);
    return {
      type: mediaType.replace("MessageMedia", "").toLowerCase(),
      filename,
      path: filepath,
      mimeType,
      size: buffer.length,
    };
  }

  if (mediaType === "MessageMediaWebPage") {
    return {
      type: "webpage",
      url: webpageValue(message.media, "url"),
      title: webpageValue(message.media, "title"),
      description: webpageValue(message.media, "description"),
    };
  }

  return false;
}

const handleWatchedEntry = createWatchedEntryHandler({
  pushMessage,
  saveTelegramMessage,
});

// --- Telegram ---
async function createClient(apiId, apiHash, sessionStr) {
  installTelegramReconnectBackoff(MTProtoSender);
  const session = new StringSession(sessionStr || "");
  const clientOptions = buildTelegramClientOptions();
  const c = new TelegramClient(session, Number(apiId), apiHash, clientOptions);
  console.log(`[watcher] Client created with Telegram proxy ${describeTelegramProxy(clientOptions)}`);
  return c;
}

async function startListening() {
  const cfg = loadConfig();
  if (!cfg.apiId || !cfg.apiHash || !cfg.session) {
    return;
  }
  if (startingListener || watcherExiting) {
    return;
  }
  startingListener = true;
  watcherMonitoring = true;
  disconnectedSince = Date.now();
  hasConnected = false;

  try {
    clearInterval(pollTimer);
    if (client) {
      await client.destroy();
    }
    client = await createClient(cfg.apiId, cfg.apiHash, cfg.session);
    const listeningClient = client;

    // Register event handlers BEFORE connecting
    // Debug: log ALL raw updates (except connection state / user status)
    client.addEventHandler((update) => {
      if (!watcherMonitoring || client !== listeningClient || watcherExiting) {
        return;
      }
      lastUpdateAt = Date.now();
      observeConnection();
      const name = updateName(update);
      if (name !== "UpdateConnectionState" && name !== "UpdateUserStatus") {
        console.log("[raw-update]", name);
      }
    });

    // Handle new messages from raw updates (more reliable than NewMessage event)
    client.addEventHandler(async (update) => {
      if (!watcherMonitoring || client !== listeningClient || watcherExiting) {
        return;
      }
      try {
        const name = update.className;
        let message = null;

        if (name === "UpdateNewMessage" || name === "UpdateNewChannelMessage") {
          message = update.message;
        } else if (name === "UpdateShortMessage" || name === "UpdateShortChatMessage") {
          // Short updates don't have full message object
          console.log("[debug] Short update:", name, "text:", shortUpdateText(update));
          return;
        } else {
          return;
        }

        if (!message || !message.peerId) {
          return;
        }

        const chatId = chatIdFromPeerId(message.peerId);
        if (!chatId) {
          return;
        }

        console.log(`[debug] msg from chatId=${chatId}`);

        // Reload watch list every time
        const currentCfg = loadConfig();
        const watchGroups = currentCfg.watchGroups || [];

        if (!chatIdMatchesWatchList(chatId, watchGroups)) {
          console.log(`[debug] FILTERED OUT chatId=${chatId}`);
          return;
        }

        const text = message.message || "";

        let senderName = "";
        try {
          if (message.fromId) {
            const senderId = message.fromId.userId || message.fromId.channelId;
            if (senderId) {
              const entity = await listeningClient.getEntity(senderId);
              senderName = entity.firstName || entity.title || entity.username || "";
              if (entity.lastName) {
                senderName += " " + entity.lastName;
              }
            }
          }
        } catch {}

        let chatTitle = chatId;
        try {
          const peerId = message.peerId.channelId || message.peerId.chatId;
          if (peerId) {
            const chat = await listeningClient.getEntity(peerId);
            chatTitle = chat.title || chat.username || chatId;
          }
        } catch {}

        let mediaInfo = false;
        try {
          mediaInfo = await loadMessageMedia(listeningClient, message);
        } catch (err) {
          console.log(`[media] Download failed: ${safeErrorMessage(err)}`);
        }

        const entry = {
          id: message.id,
          chatId,
          chatTitle,
          sender: senderName,
          text,
          media: mediaInfo,
          date: new Date(message.date * 1000).toISOString(),
        };

        if (!watcherMonitoring || client !== listeningClient || watcherExiting) {
          return;
        }
        handleWatchedEntry(entry);
        console.log(`[${chatTitle}] ${senderName}: ${text || "(media)"}`);
      } catch (err) {
        console.error("[handler] Error:", safeErrorMessage(err));
      }
    });

    await client.connect();
    if (!watcherMonitoring || watcherExiting) {
      return;
    }

    const authorized = await client.checkAuthorization();
    if (!authorized) {
      console.error("[watcher] Session not authorized! Need to re-login.");
      return;
    }
    console.log("[watcher] Authorized ✓");
    connected = isTelegramConnected();
    hasConnected = connected;
    disconnectedSince = false;

    // Fetch dialogs to prime the entity cache and activate updates
    console.log("[watcher] Fetching dialogs to activate updates...");
    const dialogs = await client.getDialogs({ limit: 30 });
    console.log(`[watcher] Got ${dialogs.length} dialogs`);

    // Force update state sync
    try {
      const { Api } = require("telegram/tl");
      const state = await client.invoke(new Api.updates.GetState());
      console.log("[watcher] Update state: pts=" + state.pts + " seq=" + state.seq + " date=" + state.date);
      
      // Get difference to catch up
      const diff = await client.invoke(new Api.updates.GetDifference({
        pts: state.pts,
        date: state.date,
        qts: state.qts || 0,
      }));
      console.log("[watcher] GetDifference:", diff.className);
    } catch (err) {
      console.log("[watcher] Update sync error:", safeErrorMessage(err));
    }

    console.log("[watcher] Connected, listening...");

    // --- Polling: periodically fetch new messages from watched groups ---
    const lastSeenIds = {}; // chatId -> last message id
    let pollInFlight = false;
    
    async function pollGroups() {
      if (!watcherMonitoring || pollInFlight || watcherExiting || client !== listeningClient || !isTelegramConnected()) {
        return;
      }
      const currentCfg = loadConfig();
      const watchGroups = currentCfg.watchGroups || [];
      if (watchGroups.length === 0) {
        return;
      }

      pollInFlight = true;
      try {
        await pollWatchedGroups(watchGroups);
      } finally {
        pollInFlight = false;
      }
    }

    async function pollWatchedGroups(watchGroups) {
      for (const groupId of watchGroups) {
        try {
          const entity = await listeningClient.getEntity(groupId);
          const pollChatId = chatIdFromEntity(entity, groupId);
          if (!pollChatId) {
            continue;
          }
          const messages = await listeningClient.getMessages(entity, { limit: 5 });
          if (!watcherMonitoring || client !== listeningClient || watcherExiting) {
            return;
          }
          lastUpdateAt = Date.now();
          
          for (const message of messages.reverse()) {
            const msgKey = `${pollChatId}:${message.id}`;
            if (lastSeenIds[msgKey]) {
              continue;
            }
            lastSeenIds[msgKey] = true;

            const messageAt = Number(message.date) * 1000;
            if (!Number.isFinite(messageAt) || Date.now() - messageAt > POLL_MAX_AGE_MS) {
              console.debug(`[debug] Skipping stale poll message chatId=${pollChatId} id=${message.id}`);
              continue;
            }

            // Skip if we already have this in our buffer
            if (watchedMessages.some(m => m.id === message.id && m.chatId === pollChatId)) {
              continue;
            }

            let senderName = "";
            try {
              const sender = await message.getSender();
              if (sender) {
                senderName = sender.firstName || sender.title || sender.username || "";
                if (sender.lastName) {
                  senderName += " " + sender.lastName;
                }
              }
            } catch {}

            let chatTitle = entity.title || entity.username || pollChatId;

            let mediaInfo = false;
            try {
              mediaInfo = await loadMessageMedia(listeningClient, message);
            } catch (err) {
              console.log(`[media] Download failed: ${safeErrorMessage(err)}`);
            }

            const entry = {
              id: message.id,
              chatId: pollChatId,
              chatTitle,
              sender: senderName,
              text: message.text || "",
              media: mediaInfo,
              date: new Date(message.date * 1000).toISOString(),
            };

            if (!watcherMonitoring || client !== listeningClient || watcherExiting) {
              return;
            }
            handleWatchedEntry(entry);
            console.log(`[poll] [${chatTitle}] ${senderName}: ${(message.text || "(media)").substring(0, 80)}`);
          }
        } catch (err) {
          console.log(`[poll] Error fetching watched group: ${safeErrorMessage(err)}`);
        }
      }
    }

    // Initial poll to seed recent messages
    pollTimer = setInterval(pollGroups, 15000);
    await pollGroups();

  } catch (err) {
    console.error("[watcher] Connection error:", safeErrorMessage(err));
    connected = false;
  } finally {
    startingListener = false;
  }
}

// --- API Routes ---

app.get("/healthz", (req, res) => {
  const isConnected = isTelegramConnected();
  let statusCode = 200;
  if (watcherExiting || !isConnected || Date.now() - lastUpdateAt > HEALTH_STALE_MS) {
    statusCode = 503;
  }
  res.status(statusCode).json({ lastUpdateAt, connected: isConnected });
});

// Get current status
app.get("/api/status", (req, res) => {
  const cfg = loadConfig();
  res.json({
    configured: !!(cfg.apiId && cfg.apiHash),
    loggedIn: !!(cfg.session),
    connected,
    watchGroups: cfg.watchGroups || [],
  });
});

// Save api credentials
app.post("/api/config", (req, res) => {
  const { apiId, apiHash } = req.body;
  if (!apiId || !apiHash) {
    return res.status(400).json({ error: "Missing apiId or apiHash" });
  }
  const cfg = loadConfig();
  cfg.apiId = apiId;
  cfg.apiHash = apiHash;
  saveConfig(cfg);
  res.json({ ok: true });
});

// Start login - send code
app.post("/api/login/start", async (req, res) => {
  const { phoneNumber } = req.body;
  const cfg = loadConfig();
  if (!cfg.apiId || !cfg.apiHash) {
    return res.status(400).json({ error: "Configure API credentials first" });
  }

  try {
    client = await createClient(cfg.apiId, cfg.apiHash, "");
    await client.connect();

    // We use start() with callbacks that we control via promises
    let phoneCodeResolve;
    let passwordResolve;
    const phoneCodePromise = new Promise((r) => (phoneCodeResolve = r));
    const passwordPromise = new Promise((r) => (passwordResolve = r));

    loginState = {
      phoneCodeResolve,
      passwordResolve,
      phase: "code",
      done: false,
    };

    // Run client.start in background
    client
      .start({
        phoneNumber: () => Promise.resolve(phoneNumber),
        phoneCode: () => {
          loginState.phase = "code";
          return phoneCodePromise;
        },
        password: () => {
          loginState.phase = "password";
          return passwordPromise;
        },
        onError: (err) => console.error("[login]", safeErrorMessage(err)),
      })
      .then(() => {
        const sessionStr = client.session.save();
        cfg.session = sessionStr;
        saveConfig(cfg);
        loginState.done = true;
        connected = true;
        console.log("[watcher] Login successful, session saved.");
      })
      .catch((err) => {
        console.error("[login] Failed:", safeErrorMessage(err));
        loginState = null;
      });

    // Give it a moment to request the code
    await new Promise((r) => setTimeout(r, 2000));

    let phase = "code";
    if (loginState && loginState.phase) {
      phase = loginState.phase;
    }
    res.json({ ok: true, phase });
  } catch (err) {
    res.status(500).json({ error: "Internal server error" });
  }
});

// Submit verification code
app.post("/api/login/code", (req, res) => {
  const { code } = req.body;
  if (!loginState || !loginState.phoneCodeResolve) {
    return res.status(400).json({ error: "No pending login" });
  }
  loginState.phoneCodeResolve(code);
  res.json({ ok: true });
});

// Submit 2FA password
app.post("/api/login/password", (req, res) => {
  const { password } = req.body;
  if (!loginState || !loginState.passwordResolve) {
    return res.status(400).json({ error: "No pending login" });
  }
  loginState.passwordResolve(password);
  res.json({ ok: true });
});

// Check login status
app.get("/api/login/status", (req, res) => {
  if (!loginState) {
    return res.json({ phase: "none" });
  }
  res.json({ phase: loginState.phase, done: loginState.done || false });
});

// Start QR login (scan with the Telegram app: 设置 -> 设备 -> 连接桌面设备)
app.post("/api/login/qr/start", async (req, res) => {
  const cfg = loadConfig();
  if (!cfg.apiId || !cfg.apiHash) {
    return res.status(400).json({ error: "Configure API credentials first" });
  }

  try {
    if (qrLoginState && qrLoginState.client && !qrLoginState.done) {
      try { await qrLoginState.client.disconnect(); } catch {}
    }

    const qrClient = await createClient(cfg.apiId, cfg.apiHash, "");
    await qrClient.connect();

    let passwordResolve;
    const passwordPromise = new Promise((r) => (passwordResolve = r));
    qrLoginState = { url: "", phase: "qr", done: false, error: "", passwordResolve, client: qrClient };
    const state = qrLoginState;

    qrClient
      .signInUserWithQrCode(
        { apiId: Number(cfg.apiId), apiHash: cfg.apiHash },
        {
          qrCode: async (code) => {
            // gramjs re-fires this whenever the token rotates (~30s)
            state.url = `tg://login?token=${code.token.toString("base64url")}`;
          },
          password: async () => {
            state.phase = "password";
            return passwordPromise;
          },
          onError: (err) => {
            console.error("[qr-login]", safeErrorMessage(err));
            state.error = safeErrorMessage(err);
            return true;
          },
        },
      )
      .then(async () => {
        const latest = loadConfig();
        latest.session = qrClient.session.save();
        saveConfig(latest);
        state.done = true;
        state.phase = "done";
        console.log("[watcher] QR login successful, session saved.");
        try { await qrClient.disconnect(); } catch {}
        if (client) {
          try { await client.disconnect(); } catch {}
          client = null;
        }
        // restart listening so watch handlers attach to the fresh session
        await startListening();
      })
      .catch((err) => {
        console.error("[qr-login] Failed:", safeErrorMessage(err));
        state.error = state.error || safeErrorMessage(err);
      });

    // give gramjs a moment to produce the first token
    await new Promise((r) => setTimeout(r, 1500));
    res.json({ ok: true, phase: state.phase, hasUrl: !!state.url });
  } catch (err) {
    console.error("[qr-login] start failed:", safeErrorMessage(err));
    res.status(500).json({ error: "Internal server error" });
  }
});

// Poll QR login progress; returns the QR payload as an inline SVG data url
app.get("/api/login/qr/status", async (req, res) => {
  if (!qrLoginState) {
    return res.json({ phase: "none" });
  }

  const payload = {
    phase: qrLoginState.phase,
    done: qrLoginState.done,
    error: qrLoginState.error || "",
    url: qrLoginState.url,
    qr: "",
  };

  if (qrLoginState.url && !qrLoginState.done) {
    try {
      const QRCode = require("qrcode");
      payload.qr = await QRCode.toDataURL(qrLoginState.url, { margin: 1, width: 240 });
    } catch (err) {
      console.error("[qr-login] qr render failed:", safeErrorMessage(err));
    }
  }

  res.json(payload);
});

// Submit 2FA password for QR login
app.post("/api/login/qr/password", (req, res) => {
  const { password } = req.body;
  if (!qrLoginState || !qrLoginState.passwordResolve) {
    return res.status(400).json({ error: "No pending QR login" });
  }
  qrLoginState.passwordResolve(password);
  res.json({ ok: true });
});

// Update watch groups
app.post("/api/groups", (req, res) => {
  const { groups } = req.body; // array of chat id strings
  const cfg = loadConfig();
  cfg.watchGroups = groups || [];
  saveConfig(cfg);
  res.json({ ok: true, watchGroups: cfg.watchGroups });
});

// Get dialogs (groups/channels list)
app.get("/api/dialogs", async (req, res) => {
  if (!client || !connected) {
    return res.status(400).json({ error: "Not connected" });
  }
  try {
    const dialogs = await client.getDialogs({ limit: 100 });
    const groups = dialogs
      .filter((d) => d.isGroup || d.isChannel)
      .map((d) => ({
        id: d.id.toString(),
        title: d.title || d.name || "Unknown",
        isChannel: d.isChannel || false,
        isGroup: d.isGroup || false,
        unreadCount: d.unreadCount || 0,
      }));
    res.json({ groups });
  } catch (err) {
    res.status(500).json({ error: "Internal server error" });
  }
});

// Get recent messages
app.get("/api/messages", (req, res) => {
  const limit = parseInt(req.query.limit) || 100;
  console.log(`[api] /messages called, watchedMessages.length=${watchedMessages.length}, limit=${limit}`);
  res.json({ messages: watchedMessages.slice(-limit).reverse() });
});

// Disconnect
app.post("/api/disconnect", async (req, res) => {
  watcherMonitoring = false;
  disconnectedSince = false;
  clearInterval(pollTimer);
  if (client) {
    await client.disconnect();
    connected = false;
  }
  res.json({ ok: true });
});

// Reconnect with saved session
app.post("/api/reconnect", async (req, res) => {
  try {
    await startListening();
    res.json({ ok: true, connected });
  } catch (err) {
    res.status(500).json({ error: "Internal server error" });
  }
});

registerTradingApi(app, priceMonitor);

// --- Start ---
const PORT = 9100;
const HOST = process.env.WATCHER_HOST || "127.0.0.1";
app.listen(PORT, HOST, () => {
  console.log(`[watcher] Web UI listening on configured host, port ${PORT}`);
  ensureTelegramMessagesTable();
  // Start price monitor
  priceMonitor.start();
  // Auto-connect if session exists
  const cfg = loadConfig();
  if (cfg.session) {
    startListening();
  }
});
