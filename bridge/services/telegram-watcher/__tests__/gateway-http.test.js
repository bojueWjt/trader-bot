const assert = require('node:assert/strict');
const test = require('node:test');
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const express = require('express');
const { createAuthMiddleware } = require('../lib/auth');
const { createRequestValidation } = require('../lib/request-validation');
const env = {WATCHER_GATEWAY_TOKEN:'g'.repeat(40),WATCHER_SNAPSHOT_TOKEN:'s'.repeat(40),WATCHER_BROWSER_PROXY_TOKEN:'b'.repeat(40)};

test('T0-1/T0-4 real HTTP W1-W8 rejects invalid inputs before any configuration write', async () => {
  const temp=fs.mkdtempSync(path.join(os.tmpdir(),'watcher-http-'));
  process.env.TRADER_TRADING_DB_PATH=path.join(temp,'db');
  delete process.env.WATCHER_TRADING_DB;
  delete process.env.TRADING_DB_PATH;
  const {registerTradingApi,getTradingDb}=require('../lib/trading-api');
  const app=express();
  app.use(createAuthMiddleware(env));
  app.use(createRequestValidation());
  registerTradingApi(app,{});
  const server=await new Promise((resolve,reject)=>{const value=app.listen(0,'127.0.0.1',()=>resolve(value));value.once('error',reject);});
  const port=server.address().port;
  async function call(method, route, body, headers = {}) {
    const response=await fetch(`http://127.0.0.1:${port}`+route,{method,headers:{authorization:'Bearer '+env.WATCHER_GATEWAY_TOKEN,'x-watcher-actor':'app:risk_admin','x-watcher-token-fingerprint':'012345abcdef','content-type':'application/json',...headers},body:body === undefined ? undefined : JSON.stringify(body),signal:AbortSignal.timeout(4000)});
    return {status:response.status,body:await response.json()};
  }
  try {
    const valid={symbol:'BTCUSDT',risk_ratio:0.02,expected_revision:0,client_ref:'http-ref-0001'};
    for (const change of [{risk_ratio:'0.02'},{risk_ratio:[0.02]},{symbol:'btcusdt'},{secret_token:'sentinel'},{extra:'unknown'}]) {
      const result=await call('POST','/api/trading/risks',{...valid,...change});
      assert.equal(result.status,400);
      assert.ok(!JSON.stringify(result.body).includes('sentinel'));
    }
    assert.equal((await call('POST','/api/trading/risks',{...valid,padding:'x'.repeat(65536)})).status,413);
    assert.equal((await call('GET','/api/trading/messages?limit=1&limit=2')).body.code,'invalid_query');
    const getBody=await new Promise((resolve,reject)=>{
      const req=http.request({host:'127.0.0.1',port,path:'/api/trading/risks',method:'GET',headers:{authorization:'Bearer '+env.WATCHER_GATEWAY_TOKEN,'x-watcher-actor':'app:viewer','x-watcher-token-fingerprint':'012345abcdef','content-length':'2'}},res=>{res.resume();res.on('end',()=>resolve(res.statusCode));});
      req.on('error',reject);req.end('{}');
    });
    assert.equal(getBody,400);
    let db=getTradingDb();
    assert.equal(db.prepare('SELECT count(*) n FROM config_audit').get().n,0);db.close();
    const first=await call('POST','/api/trading/risks',valid);
    assert.equal(first.status,200);assert.equal(first.body.revision,1);
    const replay=await call('POST','/api/trading/risks',valid);
    assert.equal(replay.body.replay,true);
    db=getTradingDb();assert.equal(db.prepare('SELECT operation FROM config_audit').get().operation,'risk.upsert');db.close();
    assert.equal((await call('DELETE','/api/price-alerts/1',{client_ref:'http-ref-0002'})).status,403);
    assert.equal((await call('GET','/api/login/not-in-table')).status,403);
  } finally {
    await new Promise(resolve=>server.close(resolve));
    delete process.env.TRADER_TRADING_DB_PATH;
    fs.rmSync(temp,{recursive:true,force:true});
  }
});

test('browser can delete a created price alert through the registered HTTP route', async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'watcher-alert-route-'));
  process.env.TRADER_TRADING_DB_PATH = path.join(temp, 'db');
  process.env.TRADING_DB_PATH = process.env.TRADER_TRADING_DB_PATH;
  delete require.cache[require.resolve('../lib/trading-api')];
  delete require.cache[require.resolve('../price-monitor')];
  const { registerTradingApi } = require('../lib/trading-api');
  const monitor = require('../price-monitor');
  monitor.ensureTable();
  const app = express();
  app.use(createAuthMiddleware(env));
  app.use(createRequestValidation());
  registerTradingApi(app, monitor);
  const server = await new Promise((resolve) => {
    const listening = app.listen(0, '127.0.0.1', () => resolve(listening));
  });
  try {
    const base = `http://127.0.0.1:${server.address().port}`;
    const headers = { 'x-watcher-proxy-auth': env.WATCHER_BROWSER_PROXY_TOKEN, 'content-type': 'application/json' };
    const created = await fetch(base + '/api/price-alerts', {
      method: 'POST', headers, body: JSON.stringify({ symbol: 'BTCUSDT', target_price: 100, direction: 'above', client_ref: 'alert-route-001' }), signal: AbortSignal.timeout(4000),
    });
    const createdBody = await created.json();
    assert.equal(created.status, 200, JSON.stringify(createdBody));
    const { id } = createdBody;
    const removed = await fetch(base + `/api/price-alerts/${id}`, { method: 'DELETE', headers, body: JSON.stringify({ client_ref: 'alert-route-002' }), signal: AbortSignal.timeout(4000) });
    assert.equal(removed.status, 200);
    assert.deepEqual(await removed.json(), { ok: true });
    const db = monitor.getDb();
    try {
      assert.equal(db.prepare('SELECT count(*) AS n FROM price_alerts WHERE id = ?').get(id).n, 0);
    } finally {
      db.close();
    }
  } finally {
    await new Promise((resolve) => server.close(resolve));
    delete process.env.TRADER_TRADING_DB_PATH;
    delete process.env.TRADING_DB_PATH;
    fs.rmSync(temp, { recursive: true, force: true });
  }
});
