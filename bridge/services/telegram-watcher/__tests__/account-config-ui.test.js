const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const pagePath = path.join(__dirname, "..", "public", "index.html");
const pageSource = fs.readFileSync(pagePath, "utf8");

test("account configuration explains dynamic effective equity in Chinese", () => {
  assert.match(
    pageSource,
    /有效权益 = 当前实时实际权益 × 系数。系数保存后保持不变，仓位随盈亏动态变化。/
  );
  assert.match(
    pageSource,
    /初始化系数 = 目标有效权益 ÷ 初始化实际权益。两个初始化值只用于计算系数。/
  );
});

test("initial multiplier calculator uses operator-provided equity values", () => {
  assert.match(
    pageSource,
    /const multiplier = targetEffectiveEquity \/ initialActualEquity;/
  );
  assert.doesNotMatch(pageSource, /value=["']9000(?:\.0+)?["']/);
  assert.doesNotMatch(
    pageSource,
    /id=["']acc-capital-multiplier["'][^>]*value=["']1(?:\.0+)?["']/
  );
  assert.doesNotMatch(
    pageSource,
    /risk_capital_multiplier\s*\|\|\s*1/
  );
});

test("main accounts and subaccounts remain available as channel targets", () => {
  assert.match(pageSource, /添加主账号/);
  assert.match(pageSource, /添加子账号/);
  assert.match(pageSource, /主账号 · \$\{esc\(mainAccount\.account_id\)\}/);
  assert.match(pageSource, /子账号 · \$\{esc\(child\.account_id\)\}/);
});

test("pending multiplier accounts are visibly disabled and excluded from routing", () => {
  assert.match(pageSource, /账号已禁用/);
  assert.match(pageSource, /id=["']acc-enabled["']/);
  assert.match(pageSource, /Number\(account\.is_enabled\) === 1/);
  assert.match(pageSource, /Number\(mainAccount\.is_enabled\) === 1/);
});

test("config writes send expected_revision and client_ref from GET header", () => {
  assert.match(pageSource, /X-Config-Revision/);
  assert.match(pageSource, /pending.expected_revision = configRevisions\[configResource\]/);
  assert.match(pageSource, /client_ref: crypto\.randomUUID\(\)/);
  assert.match(pageSource, /revision_conflict/);
});

test("risk conflict refresh does not advance an old account form revision", async () => {
  const start = pageSource.indexOf("    async function api(path, opts = {}) {");
  const end = pageSource.indexOf("    function setButtonBusy", start);
  assert.ok(start > 0 && end > start);
  const sent = [];
  let riskReads = 0;
  let accountStatus = null;
  const response = (status, body, revision) => ({
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => revision === undefined ? null : String(revision) },
    json: async () => body,
  });
  const context = {
    crypto: { randomUUID: () => "ui-ref-" + (sent.length + 1) },
    fetch: async (url, opts) => {
      const method = opts.method || "GET";
      const body = opts.body ? JSON.parse(opts.body) : null;
      sent.push({ url, method, body });
      if (url === "/api/trading/accounts" && method === "GET") {
        return response(200, [], sent.filter((item) => item.url === url).length === 1 ? 5 : 6);
      }
      if (url === "/api/trading/risks" && method === "GET") {
        riskReads += 1;
        return response(200, [], riskReads === 1 ? 5 : 6);
      }
      if (url === "/api/trading/risks" && method === "POST") {
        return response(409, { code: "revision_conflict", message: "config revision changed" });
      }
      if (url === "/api/trading/accounts/account-x" && method === "PUT") {
        accountStatus = body.expected_revision === 5 ? 409 : 200;
        return response(accountStatus, accountStatus === 409
          ? { code: "revision_conflict", message: "config revision changed" }
          : { ok: true, revision: 7 });
      }
      throw new Error("unexpected request " + method + " " + url);
    },
    toast: () => {},
  };
  context.loadAccounts = () => context.api("/trading/accounts");
  context.loadChannels = () => context.api("/trading/channels");
  context.loadRisks = () => context.api("/trading/risks");
  vm.runInNewContext(
    "const configRevisions = { accounts: null, channels: null, risks: null };" +
    "const pendingConfigWrites = new Map();" + pageSource.slice(start, end),
    context
  );
  await context.api("/trading/accounts");
  await context.api("/trading/risks");
  const risk = await context.api("/trading/risks", {
    method: "POST", body: { symbol: "BTCUSDT", risk_ratio: 0.01 },
  });
  assert.equal(risk.code, "revision_conflict");
  const account = await context.api("/trading/accounts/account-x", {
    method: "PUT", body: { is_enabled: false },
  });
  assert.equal(accountStatus, 409);
  assert.equal(account.code, "revision_conflict");
  assert.equal(sent.find((item) => item.method === "POST").body.expected_revision, 5);
  assert.equal(sent.find((item) => item.method === "PUT").body.expected_revision, 5);
});

test("successful risk write does not advance an old account form revision", async () => {
  const start = pageSource.indexOf("    async function api(path, opts = {}) {");
  const end = pageSource.indexOf("    function setButtonBusy", start);
  assert.ok(start > 0 && end > start);
  const sent = [];
  const response = (status, body, revision) => ({
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => revision === undefined ? null : String(revision) },
    json: async () => body,
  });
  const context = {
    crypto: { randomUUID: () => "ui-ref-" + (sent.length + 1) },
    fetch: async (url, opts) => {
      const method = opts.method || "GET";
      const body = opts.body ? JSON.parse(opts.body) : null;
      sent.push({ url, method, body });
      if (url === "/api/trading/accounts" && method === "GET") {
        return response(200, [], 5);
      }
      if (url === "/api/trading/risks" && method === "GET") {
        return response(200, [], sent.filter((item) => item.url === url).length === 1 ? 6 : 7);
      }
      if (url === "/api/trading/risks" && method === "POST") {
        return response(200, { ok: true, revision: 7 });
      }
      if (url === "/api/trading/accounts/account-x" && method === "PUT") {
        return response(body.expected_revision === 5 ? 409 : 200,
          body.expected_revision === 5
            ? { code: "revision_conflict", message: "config revision changed" }
            : { ok: true, revision: 8 });
      }
      throw new Error("unexpected request " + method + " " + url);
    },
    toast: () => {},
  };
  context.loadAccounts = () => context.api("/trading/accounts");
  context.loadChannels = () => context.api("/trading/channels");
  context.loadRisks = () => context.api("/trading/risks");
  vm.runInNewContext(
    "const configRevisions = { accounts: null, channels: null, risks: null };" +
    "const pendingConfigWrites = new Map();" + pageSource.slice(start, end),
    context
  );
  await context.api("/trading/accounts");
  await context.api("/trading/risks");
  const risk = await context.api("/trading/risks", {
    method: "POST", body: { symbol: "BTCUSDT", risk_ratio: 0.01 },
  });
  assert.equal(risk.revision, 7);
  const account = await context.api("/trading/accounts/account-x", {
    method: "PUT", body: { is_enabled: false },
  });
  assert.equal(account.code, "revision_conflict");
  assert.equal(sent.find((item) => item.method === "POST").body.expected_revision, 6);
  assert.equal(sent.find((item) => item.method === "PUT").body.expected_revision, 5);
});

test("account edit does not prefill masked credentials and PUT omits account_id", () => {
  assert.match(pageSource, /getElementById\('acc-key'\)\.value = '';/);
  assert.match(pageSource, /getElementById\('acc-secret'\)\.value = '';/);
  assert.doesNotMatch(pageSource, /acc-key'\)\.value = account\.api_key/);
  assert.doesNotMatch(pageSource, /acc-secret'\)\.value = account\.api_secret/);
  assert.match(pageSource, /delete payload\.account_id/);
});

test("DELETE config requests send JSON body and risk bounds match server", () => {
  assert.match(pageSource, /method: 'DELETE', body: \{\}/);
  assert.match(pageSource, /default_risk_ratio <= 0 \|\| default_risk_ratio > 0\.1/);
  assert.match(pageSource, /risk_ratio <= 0 \|\| risk_ratio > 0\.1/);
});

test('groups/disconnect/reconnect send client_ref and reuse it after transport or JSON failure', async () => {
  const start = pageSource.indexOf('    async function api(path, opts = {}) {');
  const end = pageSource.indexOf('    function setButtonBusy', start);
  const sent = [];
  let mode = 'transport';
  const context = {
    crypto: {randomUUID:()=> 'site-ref-' + sent.length},
    fetch: async (url, opts) => {
      sent.push({url,body:JSON.parse(opts.body)});
      if (mode === 'transport') throw new Error('transport failed');
      return {ok:true,status:200,headers:{get:()=>null},json:async()=>{
        if (mode === 'json') throw new Error('invalid JSON');
        return {ok:true,revision:0,replay:true};
      }};
    },
    toast() {},
  };
  vm.runInNewContext('const configRevisions={};const pendingConfigWrites=new Map();' + pageSource.slice(start,end),context);
  for (const route of ['/groups','/disconnect','/reconnect']) {
    const opts={method:'POST',body:route === '/groups' ? {groups:[]} : {}};
    mode='transport';
    await assert.rejects(context.api(route,opts));
    const first=sent.at(-1).body;
    assert.ok(first.client_ref);
    assert.equal(Object.hasOwn(first,'expected_revision'),false);
    mode='json';await assert.rejects(context.api(route,opts));
    assert.equal(sent.at(-1).body.client_ref,first.client_ref);
    mode='success';await context.api(route,opts);
    assert.equal(sent.at(-1).body.client_ref,first.client_ref);
    await context.api(route,opts);
    assert.notEqual(sent.at(-1).body.client_ref,first.client_ref);
  }
});

test('disconnect and reconnect require Chinese confirmation before any request', async () => {
  const start = pageSource.indexOf('    async function changeWatcherConnection(action) {');
  const end = pageSource.indexOf('    async function saveGroups()', start);
  assert.ok(start > 0 && end > start);
  assert.match(pageSource, /changeWatcherConnection\('disconnect'\)/);
  assert.match(pageSource, /changeWatcherConnection\('reconnect'\)/);
  const sent = [];
  const prompts = [];
  let approved = false;
  const context = {
    confirm(message) { prompts.push(message); return approved; },
    api: async (route, options) => { sent.push({ route, options }); return { ok:true }; },
    checkStatus: async () => {},
    toast() {},
  };
  vm.runInNewContext(pageSource.slice(start, end), context);
  for (const action of ['disconnect', 'reconnect']) {
    await context.changeWatcherConnection(action);
    assert.equal(sent.length, 0, `${action} sent a request after cancellation`);
  }
  assert.match(prompts[0], /断开监听/);
  assert.match(prompts[0], /停止信号采集/);
  assert.match(prompts[0], /断流告警/);
  assert.match(prompts[1], /重新连接/);
  approved = true;
  for (const action of ['disconnect', 'reconnect']) {
    await context.changeWatcherConnection(action);
  }
  assert.deepEqual(sent.map(item => item.route), ['/disconnect', '/reconnect']);
});
