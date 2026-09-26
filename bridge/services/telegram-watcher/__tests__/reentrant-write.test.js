const assert = require('node:assert/strict');
const test = require('node:test');
const Database = require('better-sqlite3');
const { ensureConfigTables } = require('../lib/config-store');
const { createReentrantWriter } = require('../lib/reentrant-write');
const { PAYLOAD } = require('../lib/generated/gateway-routes');
function request(operation, ref, fields = {}, identity = 'gateway') {
  const route = PAYLOAD.routes.find(row => row.identity === identity && row.write && row.write.operation === operation);
  const req = {method:route.method,path:route.inner_path,body:{...fields,client_ref:ref}};
  Object.defineProperty(req,'watcherRoute',{value:route});
  Object.defineProperty(req,'watcherAuth',{value:Object.freeze({identity,role:identity === 'gateway' ? 'risk_admin' : null,actor:identity === 'gateway' ? 'app:risk_admin' : 'browser',tokenFingerprint:identity === 'gateway' ? '123456789abc' : null})});
  return req;
}
async function invoke(handler, req) {
  const res = {req,statusCode:200,status(code) {this.statusCode=code;return this;},json(body){this.body=body;return this;}};
  await handler(req,res);
  return res;
}
test('T0-2 all three reentrant writes audit, replay, conflict and keep revision stable', async () => {
  const db = new Database(':memory:');
  ensureConfigTables(db);
  const close = db.close.bind(db);
  db.close = () => {};
  const wrap = createReentrantWriter(() => db);
  try {
    for (const operation of ['groups.save','telegram.disconnect','telegram.reconnect']) {
      let effects = 0;
      const fields = operation === 'groups.save' ? {groups:['-100']} : {};
      const handler = wrap(async () => {assert.equal(db.inTransaction,false);effects++;return {ok:true};});
      const first = await invoke(handler, request(operation,'ref-00001',fields));
      assert.deepEqual(first.body,{ok:true,revision:0,replay:false});
      assert.equal((await invoke(handler,request(operation,'ref-00001',fields))).body.replay,true);
      assert.equal(effects,1);
      assert.equal((await invoke(handler,request(operation,'ref-00001',{...fields,changed:true}))).statusCode,409);
      assert.equal(effects,1);
      const audit = db.prepare('SELECT * FROM config_audit WHERE operation = ?').get(operation);
      assert.equal(audit.actor,'app:risk_admin');
      assert.equal(audit.source,'gateway');
      assert.equal(audit.token_fingerprint,'123456789abc');
      assert.equal(audit.revision_before,audit.revision_after);
      assert.equal(audit.operation,operation);
    }
    const browser = await invoke(wrap(async () => ({ok:true})),request('groups.save','browser-0001',{groups:[]},'browser'));
    assert.equal(browser.statusCode,200);
    assert.equal(db.prepare("SELECT token_fingerprint FROM config_audit WHERE actor='browser'").get().token_fingerprint,null);
    assert.equal(db.prepare('SELECT revision FROM config_revision').get().revision,0);
  } finally {close();}
});
test('T0-2 in-flight duplicates join; failures do not reserve a client_ref', async () => {
  const db = new Database(':memory:');
  ensureConfigTables(db);
  const close = db.close.bind(db);db.close=()=>{};
  const wrap = createReentrantWriter(()=>db);
  let release;
  let effects=0;
  const handler=wrap(async()=>{effects++;await new Promise(resolve=>{release=resolve;});return {ok:true};});
  try {
    const first=invoke(handler,request('telegram.reconnect','same-ref-001'));
    const second=invoke(handler,request('telegram.reconnect','same-ref-001'));
    release();
    const results=await Promise.all([first,second]);
    assert.equal(effects,1);
    assert.deepEqual(results.map(r=>r.body.replay),[false,true]);
    const fail=wrap(async()=>{throw new Error('private-sentinel');});
    assert.equal((await invoke(fail,request('groups.save','retry-ref-001',{groups:[]}))).statusCode,500);
    const recovered=await invoke(wrap(async()=>({ok:true})),request('groups.save','retry-ref-001',{groups:[]}));
    assert.equal(recovered.body.replay,false);
    assert.equal((await invoke(handler,{method:'POST',body:{client_ref:'missing-ctx'}})).statusCode,500);
    assert.ok(!JSON.stringify(db.prepare('SELECT * FROM config_audit').all()).includes('private-sentinel'));
  } finally {close();}
});

test('T0-2 failed audit can reapply the external effect; committed replay never reapplies it', async () => {
  const db = new Database(':memory:');
  ensureConfigTables(db);
  const close = db.close.bind(db);
  db.close = () => {};
  const wrap = createReentrantWriter(() => db);
  let effects = 0;
  const handler = wrap(async () => { effects++; return {ok:true}; });
  const req = () => request('groups.save','audit-failure-001',{groups:[]});
  try {
    db.exec("CREATE TRIGGER deny_audit BEFORE INSERT ON config_audit BEGIN SELECT RAISE(ABORT, 'fixture'); END");
    assert.equal((await invoke(handler,req())).statusCode,500);
    assert.equal(effects,1);
    assert.equal(db.prepare('SELECT count(*) n FROM config_audit').get().n,0);
    db.exec('DROP TRIGGER deny_audit');
    assert.equal((await invoke(handler,req())).body.replay,false);
    assert.equal(effects,2);
    assert.equal((await invoke(handler,req())).body.replay,true);
    assert.equal(effects,2);
  } finally { close(); }
});
