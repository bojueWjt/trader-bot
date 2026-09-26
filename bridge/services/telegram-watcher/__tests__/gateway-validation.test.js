'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const crypto = require('node:crypto');
const { Readable } = require('node:stream');
const { createAuthMiddleware } = require('../lib/auth');
const { PAYLOAD } = require('../lib/generated/gateway-routes');
const env = { WATCHER_GATEWAY_TOKEN: 'g'.repeat(40), WATCHER_SNAPSHOT_TOKEN: 's'.repeat(40), WATCHER_BROWSER_PROXY_TOKEN: 'b'.repeat(40) };
function auth(path = '/api/status', method = 'GET', extra = [], token = env.WATCHER_GATEWAY_TOKEN) {
  const req = { originalUrl: path, method, rawHeaders: ['Authorization', `Bearer ${token}`, 'X-Watcher-Actor', 'app:risk_admin', 'X-Watcher-Token-Fingerprint', '012345abcdef', ...extra] };
  const res = { statusCode: 200, setHeader() {}, status(v) { this.statusCode = v; return this; }, json(v) { this.body = v; } };
  let passed = false;
  createAuthMiddleware(env)(req, res, () => { passed = true; });
  return { req, res, passed };
}
test('T0-1 E-02 rejects whitespace/non-ASCII configured tokens without trimming', () => {
  for (const value of ['a'.repeat(31), ' ' + 'a'.repeat(40), 'a'.repeat(40) + '\n', 'é'.repeat(40), 'a'.repeat(32) + ' b', '\t']) {
    for (const name of ['WATCHER_GATEWAY_TOKEN', 'WATCHER_GATEWAY_TOKEN_PREVIOUS']) {
      assert.throws(() => createAuthMiddleware({ ...env, [name]: value }), /WATCHER_/);
    }
  }
});
test('T0-1 E-12 attaches exact generated row and immutable authentication', () => {
  const { req, passed } = auth();
  assert.equal(passed, true);
  assert.equal(req.watcherRoute, PAYLOAD.routes.find(r => r.id === 'gw.status.get'));
  for (const name of ['watcherAuth', 'watcherRoute']) {
    const descriptor = Object.getOwnPropertyDescriptor(req, name);
    assert.equal(descriptor.writable, false);
    assert.equal(descriptor.configurable, false);
    assert.equal(descriptor.enumerable, true);
    assert.ok(Object.isFrozen(req[name]));
    assert.throws(() => { req[name] = {}; }, TypeError);
  }
  assert.throws(() => req.watcherRoute.body.allow.push('injected'), TypeError);
});
test('T0-1 E-12 rejects a copied route even when its fields match the generated row', async () => {
  const { requireWatcherContext } = require('../lib/request-validation');
  const { req } = auth();
  const copied = structuredClone(req.watcherRoute);
  const forged = { watcherAuth:req.watcherAuth, watcherRoute:copied };
  assert.deepEqual(copied,req.watcherRoute);
  assert.throws(() => requireWatcherContext(forged), error => error.status === 500 && error.code === 'internal_error');
});
test('T0-1 compares every configured bearer digest, including unequal token lengths', () => {
  const original = crypto.timingSafeEqual;
  const calls = [];
  crypto.timingSafeEqual = (a, b) => { calls.push([a.length, b.length]); return original(a,b); };
  try {
    assert.equal(auth().passed, true);
    assert.deepEqual(calls, [[32,32],[32,32]]);
    calls.length = 0;
    assert.equal(auth('/api/status','GET', [], 'short').res.statusCode, 401);
    assert.deepEqual(calls, [[32,32],[32,32]]);
  } finally { crypto.timingSafeEqual = original; }
});
test('T0-1 compares every current and previous bearer digest after an early match', () => {
  const rotated = { ...env, WATCHER_GATEWAY_TOKEN_PREVIOUS: 'p'.repeat(40), WATCHER_SNAPSHOT_TOKEN_PREVIOUS: 'q'.repeat(40) };
  const original = crypto.timingSafeEqual;
  const calls = [];
  crypto.timingSafeEqual = (a, b) => { calls.push([a.length, b.length]); return original(a, b); };
  try {
    for (const token of [rotated.WATCHER_GATEWAY_TOKEN, rotated.WATCHER_GATEWAY_TOKEN_PREVIOUS, 'wrong']) {
      const req = { originalUrl: '/api/status', method: 'GET', rawHeaders: ['Authorization', `Bearer ${token}`, 'X-Watcher-Actor', 'app:viewer', 'X-Watcher-Token-Fingerprint', '012345abcdef'] };
      const res = { statusCode: 200, setHeader() {}, status(code) { this.statusCode = code; return this; }, json(body) { this.body = body; } };
      calls.length = 0;
      createAuthMiddleware(rotated)(req, res, () => {});
      assert.deepEqual(calls, [[32, 32], [32, 32], [32, 32], [32, 32]]);
    }
  } finally { crypto.timingSafeEqual = original; }
});
test('T0-4 never_allowed independently rejects unregistered paths for every method', () => {
  for (const method of ['GET','POST','PUT','DELETE','PATCH','HEAD','OPTIONS']) {
    assert.equal(auth('/api/login/unregistered', method).res.statusCode, 403);
  }
  assert.equal(auth('/').res.statusCode,403);
  assert.equal(auth('/api/price-alerts/1','DELETE').passed, false);
});
async function validate(path, method, body, options = {}) {
  const { createRequestValidation } = require('../lib/request-validation');
  const { req: context, res } = auth(path, method);
  const text = options.raw === undefined ? JSON.stringify(body) : options.raw;
  const req = Readable.from(text === undefined ? [] : [Buffer.from(text)]);
  Object.defineProperties(req, Object.getOwnPropertyDescriptors(context));
  req.headers = { 'content-type': 'application/json', ...options.headers };
  let passed = false;
  await createRequestValidation()(req, res, () => { passed = true; });
  return { req, res, passed };
}
test('T0-4 W6 rejects duplicate/unknown/invalid/partial cursor query', async () => {
  for (const query of ['limit=2&limit=3','token=secret','hours=0','limit=501','before_id=1','before_created_at=x&before_id=2']) {
    const result = await validate('/api/trading/messages?' + query,'GET');
    assert.equal(result.res.body.code, 'invalid_query');
  }
  assert.equal((await validate('/api/trading/messages?hours=1&limit=2','GET')).passed, true);
});
test('T0-1 W7 enforces size, object, secret, allow, required and strict gateway types', async () => {
  const valid = { symbol:'BTCUSDT',risk_ratio:0.02,expected_revision:0,client_ref:'test-ref-0001' };
  assert.equal((await validate('/api/trading/risks','POST',valid)).passed,true);
  for (const bad of [{risk_ratio:'0.02'}, {risk_ratio:[0.02]}, {symbol:'btcusdt'}, {extra:true}, {client_ref:undefined}]) {
    assert.equal((await validate('/api/trading/risks','POST',{...valid,...bad})).res.body.code,'invalid_body');
  }
  const definedButDisallowed = await validate('/api/trading/risks','POST',{...valid,channel_id:'-100'});
  assert.deepEqual(definedButDisallowed.res.body.details.unknown_fields,['channel_id']);
  for (const key of ['api_key','apİ_key','nested']) {
    const val = key === 'nested' ? {token:'sentinel'} : 'sentinel';
    const result = await validate('/api/trading/risks','POST',{...valid,[key]:val});
    assert.equal(result.res.body.code,'secret_field_rejected');
    assert.ok(!JSON.stringify(result.res.body).includes('sentinel'));
  }
  assert.equal((await validate('/api/trading/accounts/a','PUT',{expected_revision:0,client_ref:'test-ref-0001',is_enabled:'true'})).res.body.code,'invalid_body');
  assert.equal((await validate('/api/status','GET',{})).res.body.code,'invalid_body');
  assert.equal((await validate('/api/trading/risks','POST',[],{})).res.body.code,'invalid_body');
  assert.equal((await validate('/api/trading/risks','POST',undefined,{raw:' '.repeat(65537)})).res.statusCode,413);
  assert.equal((await validate('/api/trading/risks','POST',valid,{headers:{'content-length':'65537'}})).res.statusCode,413);
});

test('T0-1 W7 uses generated browser account field override for non-ASCII account IDs', async () => {
  const route = PAYLOAD.routes.find(row => row.id === 'br.accounts.post');
  assert.equal(route.body.field_overrides.account_id, 'account_id_credential');
  const { createRequestValidation } = require('../lib/request-validation');
  const body = { account_id:'账户甲', api_key:'fake-key', api_secret:'fake-secret', risk_capital_addon:0, expected_revision:0, client_ref:'browser-ref-0001' };
  const req = Readable.from([Buffer.from(JSON.stringify(body))]);
  Object.defineProperty(req, 'watcherAuth', { value: Object.freeze({ identity:'browser',role:null,actor:'browser',tokenFingerprint:null }) });
  Object.defineProperty(req, 'watcherRoute', { value: route });
  req.method = 'POST';
  req.originalUrl = route.inner_path;
  req.headers = { 'content-type':'application/json' };
  const res = { statusCode:200, status(code) { this.statusCode=code; return this; }, json(value) { this.body=value; } };
  let passed = false;
  await createRequestValidation()(req,res,()=>{passed=true;});
  assert.equal(passed,true,JSON.stringify(res.body));
  assert.equal(req.body.account_id,'账户甲');
});

test('E-02 Basic scheme is case insensitive and bearer stripping matches Python whitespace', () => {
  for (const scheme of ['Basic','basic','BASIC','bAsIc']) {
    const req={originalUrl:'/healthz',method:'GET',rawHeaders:['Authorization',scheme+' ignored','X-Watcher-Proxy-Auth',env.WATCHER_BROWSER_PROXY_TOKEN]};
    let passed=false;
    createAuthMiddleware(env)(req,{status(){throw new Error('unexpected denial');}},()=>{passed=true;});
    assert.equal(passed,true);
  }
  for (const whitespace of ['\x1c','\x1d','\x1e','\x1f','\x85','\xa0','\u1680','\u2000','\u2029','\u3000']) {
    assert.equal(auth('/api/status','GET',[],whitespace+env.WATCHER_GATEWAY_TOKEN+whitespace).passed,true);
  }
  assert.equal(auth('/api/status','GET',[],'\ufeff'+env.WATCHER_GATEWAY_TOKEN).res.statusCode,401);
});
