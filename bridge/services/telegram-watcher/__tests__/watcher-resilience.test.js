const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const { createRequire } = require("node:module");

const serverPath = path.join(__dirname, "..", "server.js");
const source = fs.readFileSync(serverPath, "utf8");
const serverRequire = createRequire(serverPath);
const START = Date.parse("2026-09-04T12:00:00Z");
const MINUTE = 60000;

async function settle() {
  for (let round = 0; round < 60; round += 1) {
    await Promise.resolve();
  }
}

async function boot(options = {}) {
  let now = START;
  let timerId = 0;
  const timers = new Map();
  const routes = new Map();
  const handlers = [];
  const saved = [];
  const alerts = [];
  const logs = [];
  const exits = [];
  const cfg = { apiId: "1", apiHash: "test", session: "test", watchGroups: ["123"] };
  const state = { connectCalls: 0, messageCalls: 0, probeCalls: 0, destroyCalls: 0 };
  const client = {
    connected: false,
    _reconnecting: false,
    _sender: { isReconnecting: false, userDisconnected: false },
    addEventHandler(handler) {
      handlers.push(handler);
    },
    async connect() {
      state.connectCalls += 1;
      if (options.connect) {
        return options.connect(client);
      }
      client.connected = true;
      if (options.connectionUpdate) {
        await Promise.all(handlers.map((handler) => handler({ className: "UpdateConnectionState" })));
      }
    },
    async disconnect() {
      client.connected = false;
    },
    async destroy() {
      state.destroyCalls += 1;
      client.connected = false;
    },
    async checkAuthorization() {
      return true;
    },
    async getDialogs() {
      return [];
    },
    async invoke() {
      return { pts: 1, seq: 1, date: now / 1000, className: "Updates" };
    },
    async getEntity(groupId) {
      return { className: "Channel", id: groupId, title: "Test channel" };
    },
    async getMessages(entity) {
      if (entity === "me") {
        state.probeCalls += 1;
        if (options.probe) {
          return options.probe();
        }
        return [];
      }
      state.messageCalls += 1;
      if (options.poll) {
        return options.poll(entity);
      }
      return [...(options.messages || [])];
    },
  };
  function schedule(callback, delay, repeat) {
    timerId += 1;
    timers.set(timerId, { callback, delay, repeat, due: now + delay });
    return timerId;
  }
  const app = {
    use() {},
    get(route, handler) {
      routes.set(`GET ${route}`, handler);
    },
    post(route, handler) {
      routes.set(`POST ${route}`, handler);
    },
    listen(port, host, handler) {
      handler();
    },
  };
  const express = Object.assign(() => app, { json() {}, static() {} });
  const fakeFs = {
    existsSync() {
      return true;
    },
    readFileSync(filename) {
      if (filename.endsWith("config.json")) {
        return JSON.stringify(cfg);
      }
      return "[]";
    },
    writeFileSync() {},
  };
  class ClockDate extends Date {
    static now() {
      return now;
    }
  }
  const context = vm.createContext({
    __dirname: path.dirname(serverPath),
    Date: ClockDate,
    AbortSignal,
    process: { env: options.env || {}, on() {}, exit(code) { exits.push(code); } },
    console: Object.fromEntries(["log", "error", "debug", "warn"].map((name) => [name, (...args) => logs.push(args.join(" "))])),
    setInterval(callback, delay) {
      return schedule(callback, delay, true);
    },
    clearInterval(id) {
      timers.delete(id);
    },
    setTimeout(callback, delay) {
      return schedule(callback, delay, false);
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    async fetch(url, request) {
      alerts.push({ url, ...JSON.parse(request.body) });
      if (options.fetch) {
        return options.fetch();
      }
      return { ok: true, async json() { return { ok: true }; } };
    },
    require(name) {
      if (name === "express") {
        return express;
      }
      if (name === "fs") {
        return fakeFs;
      }
      if (name === "telegram") {
        return { TelegramClient: function TelegramClient() { return client; }, Api: {} };
      }
      if (name === "telegram/sessions") {
        return { StringSession: function StringSession() {} };
      }
      if (name === "telegram/events") {
        return { NewMessage: function NewMessage() {} };
      }
      if (name === "telegram/network/MTProtoSender") {
        return { MTProtoSender: class MTProtoSender { async _reconnect() {} } };
      }
      if (name === "telegram/tl") {
        return { Api: { updates: { GetState: function GetState() {}, GetDifference: function GetDifference() {} } } };
      }
      if (name === "./price-monitor") {
        return { start() {}, stop() {} };
      }
      if (name === "./lib/trading-api") {
        return { ensureTelegramMessagesTable() {}, registerTradingApi() {}, saveTelegramMessage(entry) { saved.push(entry); } };
      }
      return serverRequire(name);
    },
  });
  vm.runInContext(source, context, { filename: serverPath });
  await settle();
  return {
    client, state, saved, alerts, logs, exits, cfg, timers,
    async advance(milliseconds) {
      now += milliseconds;
      for (const [id, timer] of [...timers]) {
        if (timer.due > now || !timers.has(id)) {
          continue;
        }
        if (timer.repeat) {
          timer.due = now + timer.delay;
        } else {
          timers.delete(id);
        }
        timer.callback();
      }
      await settle();
    },
    async raw(update = { className: "UpdateUserStatus" }) {
      await Promise.all(handlers.map((handler) => handler(update)));
      await settle();
    },
    async request(route) {
      const handler = routes.get(route);
      assert.equal(typeof handler, "function", `${route} must be registered`);
      const response = { statusCode: 200, status(code) { this.statusCode = code; return this; }, json(body) { this.body = body; } };
      await handler({ body: {} }, response);
      await settle();
      return response;
    },
  };
}

function message(id, ageMinutes) {
  return { id, date: (START - ageMinutes * MINUTE) / 1000, text: "signal", async getSender() { return false; } };
}

test("cold poll rejects old and invalid dates before SQLite INSERT; later polls deduplicate", async () => {
  const watcher = await boot({ messages: [message(4, 0), message(3, 30), message(2, 31), message(1, NaN)] });
  assert.deepEqual(watcher.saved.map((entry) => entry.id), [3, 4]);
  await watcher.advance(15000);
  assert.deepEqual(watcher.saved.map((entry) => entry.id), [3, 4]);
  assert.equal(watcher.state.messageCalls, 2);
  assert.match(watcher.logs.join("\n"), /\[debug\].*stale/i);
});

test("newly added channels also get the cold poll age gate", async () => {
  const watcher = await boot({ messages: [message(2, 5), message(1, 45)] });
  watcher.cfg.watchGroups.push("456");
  await watcher.advance(15000);
  assert.deepEqual(watcher.saved.map((entry) => entry.id), [2, 2]);
  assert.equal(new Set(watcher.saved.map((entry) => entry.chatId)).size, 2);
});

test("GramJS userDisconnected with connected=true still exits after 120s", async () => {
  const watcher = await boot();
  watcher.client.connected = true;
  watcher.client._reconnecting = false;
  watcher.client._sender.isReconnecting = false;
  watcher.client._sender.userDisconnected = true;
  await watcher.raw({ className: "UpdateConnectionState" });
  await watcher.advance(119999);
  assert.deepEqual(watcher.exits, []);
  await watcher.advance(5000);
  assert.deepEqual(watcher.exits, [1]);
  await watcher.advance(MINUTE);
  assert.deepEqual(watcher.exits, [1]);
  assert.equal(watcher.state.connectCalls, 1);
});

test("120 seconds disconnected explicitly exits once, independently of internal reconnect", async () => {
  const watcher = await boot();
  watcher.client.connected = false;
  watcher.client._reconnecting = true;
  watcher.client._sender.isReconnecting = true;
  await watcher.raw({ className: "UpdateConnectionState" });
  await watcher.advance(119999);
  assert.deepEqual(watcher.exits, []);
  await watcher.advance(5000);
  assert.deepEqual(watcher.exits, [1]);
  await watcher.advance(MINUTE);
  assert.deepEqual(watcher.exits, [1]);
  assert.equal(watcher.state.connectCalls, 1);
});

test("watchdog covers a hung initial connection", async () => {
  const watcher = await boot({ connect: () => new Promise(() => {}) });
  await watcher.advance(2 * MINUTE);
  assert.deepEqual(watcher.exits, [1]);
});

test("watchdog runs during a hung initial poll, and stale failed probe exits", async () => {
  const watcher = await boot({ poll: () => new Promise(() => {}), probe: () => Promise.reject(new Error("offline")) });
  await watcher.advance(20 * MINUTE + 1);
  assert.equal(watcher.state.probeCalls, 1);
  assert.deepEqual(watcher.exits, [1]);
  assert.equal(watcher.state.messageCalls, 1);
});

test("stale hanging probe has a deadline and exits", async () => {
  const watcher = await boot({ poll: () => new Promise(() => {}), probe: () => new Promise(() => {}) });
  await watcher.advance(20 * MINUTE + 1);
  assert.deepEqual(watcher.exits, []);
  await watcher.advance(10000);
  assert.deepEqual(watcher.exits, [1]);
  assert.equal(watcher.state.probeCalls, 1);
});

test("successful idle probe refreshes liveness", async () => {
  const watcher = await boot({ poll: () => new Promise(() => {}) });
  await watcher.advance(20 * MINUTE + 1);
  assert.equal(watcher.state.probeCalls, 1);
  assert.deepEqual(watcher.exits, []);
  const response = await watcher.request("GET /healthz");
  assert.equal(response.statusCode, 200);
});

test("health uses actual connectivity and raw updates; empty successful polls refresh liveness", async () => {
  const watcher = await boot();
  await watcher.advance(16 * MINUTE);
  let response = await watcher.request("GET /healthz");
  assert.equal(response.statusCode, 200);
  assert.equal(response.body.lastUpdateAt, START + 16 * MINUTE);
  watcher.client.connected = false;
  await watcher.raw();
  response = await watcher.request("GET /healthz");
  assert.equal(response.body.connected, false);
  assert.equal(response.body.lastUpdateAt, START + 16 * MINUTE);
});

test("health is 503 after 15 minutes without updates", async () => {
  const watcher = await boot({ poll: () => new Promise(() => {}) });
  await watcher.advance(15 * MINUTE + 1);
  const response = await watcher.request("GET /healthz");
  assert.equal(response.statusCode, 503);
  assert.equal(response.body.connected, true);
  assert.equal(response.body.lastUpdateAt, START);
});

test("recovery alerts once and resets continuous disconnect duration", async () => {
  const watcher = await boot({ env: { WATCHER_ALERT_BOT_TOKEN: "test-token", WATCHER_ALERT_CHAT_ID: "test-chat" } });
  watcher.client.connected = false;
  await watcher.raw();
  await watcher.advance(MINUTE);
  watcher.client.connected = true;
  await watcher.raw();
  await watcher.advance(MINUTE);
  assert.deepEqual(watcher.exits, []);
  assert.equal(watcher.alerts.length, 1);
  assert.match(watcher.alerts[0].text, /reconnected/i);
  assert.equal(watcher.alerts[0].chat_id, "test-chat");
  watcher.client.connected = false;
  await watcher.raw();
  await watcher.advance(2 * MINUTE);
  assert.deepEqual(watcher.exits, [1]);
  assert.equal(watcher.alerts.length, 3);
  assert.match(watcher.alerts[1].text, /disconnected/i);
  assert.match(watcher.alerts[2].text, /exit/i);
});

test("hanging alert delivery cannot block process exit", async () => {
  const watcher = await boot({
    env: { WATCHER_ALERT_BOT_TOKEN: "test-token", WATCHER_ALERT_CHAT_ID: "test-chat" },
    fetch: () => new Promise(() => {}),
  });
  watcher.client.connected = false;
  await watcher.raw();
  await watcher.advance(2 * MINUTE);
  await watcher.advance(5000);
  assert.deepEqual(watcher.exits, [1]);
  assert.equal(watcher.alerts.length, 2);
});

test("watchdog performs no manual connect even when internal reconnect is idle", async () => {
  const watcher = await boot();
  watcher.client.connected = false;
  await watcher.raw();
  await watcher.advance(MINUTE);
  assert.equal(watcher.state.connectCalls, 1);
  await watcher.advance(MINUTE);
  assert.deepEqual(watcher.exits, [1]);
});

test("a stuck internal reconnect is disconnected even while GramJS reports connected", async () => {
  const watcher = await boot();
  watcher.client._sender.isReconnecting = true;
  await watcher.raw();
  const response = await watcher.request("GET /healthz");
  assert.equal(response.body.connected, false);
  await watcher.advance(2 * MINUTE);
  assert.deepEqual(watcher.exits, [1]);
});

test("raw update restores health after stale polling", async () => {
  const watcher = await boot({ poll: () => new Promise(() => {}) });
  await watcher.advance(16 * MINUTE);
  await watcher.raw();
  const response = await watcher.request("GET /healthz");
  assert.equal(response.statusCode, 200);
  assert.equal(response.body.lastUpdateAt, START + 16 * MINUTE);
});

test("fresh raw data during a pending failed probe prevents stale exit", async () => {
  let failProbe;
  const watcher = await boot({
    poll: () => new Promise(() => {}),
    probe: () => new Promise((resolve, reject) => { failProbe = reject; }),
  });
  await watcher.advance(20 * MINUTE + 1);
  await watcher.raw();
  failProbe(new Error("old probe failure"));
  await settle();
  assert.deepEqual(watcher.exits, []);
});

test("initial connection update produces no recovery alert", async () => {
  const watcher = await boot({
    connectionUpdate: true,
    env: { WATCHER_ALERT_BOT_TOKEN: "test-token", WATCHER_ALERT_CHAT_ID: "test-chat" },
  });
  assert.deepEqual(watcher.alerts, []);
});

test("partial watcher alert configuration only logs and still exits", async () => {
  const watcher = await boot({
    env: { WATCHER_ALERT_BOT_TOKEN: "test-token", WATCHER_ALERT_CHAT_ID: " ", TELEGRAM_HOME_CHANNEL: "other", TELEGRAM_BOT_TOKEN: "other" },
  });
  watcher.client.connected = false;
  await watcher.raw();
  await watcher.advance(2 * MINUTE);
  assert.deepEqual(watcher.alerts, []);
  assert.deepEqual(watcher.exits, [1]);
  assert.match(watcher.logs.join("\n"), /missing WATCHER_ALERT/);
  assert.doesNotMatch(watcher.logs.join("\n"), /test-token/);
});

test("failed alert delivery only logs and still exits", async () => {
  const watcher = await boot({
    env: { WATCHER_ALERT_BOT_TOKEN: "test-token", WATCHER_ALERT_CHAT_ID: "test-chat" },
    fetch: () => Promise.reject(new Error("https://api.telegram.org/bottest-token/sendMessage")),
  });
  watcher.client.connected = false;
  await watcher.raw();
  await watcher.advance(2 * MINUTE);
  assert.deepEqual(watcher.exits, [1]);
  assert.match(watcher.logs.join("\n"), /Alert delivery failed/);
  assert.doesNotMatch(watcher.logs.join("\n"), /test-token/);
});

test("explicit disconnect disables watchdog and ignores an outstanding failed probe", async () => {
  let failProbe;
  const watcher = await boot({
    poll: () => new Promise(() => {}),
    probe: () => new Promise((resolve, reject) => { failProbe = reject; }),
  });
  await watcher.advance(20 * MINUTE + 1);
  await watcher.request("POST /api/disconnect");
  failProbe(new Error("operator disconnected"));
  await settle();
  await watcher.advance(2 * MINUTE);
  assert.deepEqual(watcher.exits, []);
});

test("repeated API reconnects retire the old client and retain one poll timer", async () => {
  const watcher = await boot();
  await watcher.request("POST /api/reconnect");
  assert.equal(watcher.state.destroyCalls, 1);
  const pollingTimers = [...watcher.timers.values()].filter((timer) => timer.repeat && timer.delay === 15000);
  assert.equal(pollingTimers.length, 1);
});
