const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const { Readable } = require('node:stream');

const Database = require('better-sqlite3');
const { PAYLOAD } = require('../lib/generated/gateway-routes');
const { createRequestValidation } = require('../lib/request-validation');

const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'watcher-read-model-'));
const dbPath = path.join(directory, 'trading.db');
const previousPath = process.env.TRADING_DB_PATH;
const previousTraderPath = process.env.TRADER_TRADING_DB_PATH;
const previousWatcherPath = process.env.WATCHER_TRADING_DB;
const handlers = new Map();

function insertRows(table, count, ageHours, channel = '-100') {
  const db = new Database(dbPath);
  try {
    const timestamp = db.prepare("SELECT datetime('now', '-' || ? || ' hours') AS value").get(ageHours).value;
    const sql = table === 'briefings'
      ? 'INSERT INTO briefings (channel_id, content, created_at) VALUES (?, ?, ?)'
      : 'INSERT INTO telegram_messages (channel_id, text, created_at) VALUES (?, ?, ?)';
    const insert = db.prepare(sql);
    const ids = [];
    const transaction = db.transaction(() => {
      for (let index = 0; index < count; index += 1) {
        ids.push(Number(insert.run(channel, `${table}-${index}`, timestamp).lastInsertRowid));
      }
    });
    transaction();
    return ids;
  } finally {
    db.close();
  }
}

async function read(table, query = '') {
  const pathname = `/api/trading/${table}`;
  const originalUrl = pathname + query;
  const req = Readable.from([]);
  req.method = 'GET';
  req.originalUrl = originalUrl;
  req.query = Object.fromEntries(new URLSearchParams(query.slice(1)));
  req.headers = {};
  const route = PAYLOAD.routes.find(row => row.identity === 'browser'
    && row.method === 'GET' && row.inner_path === pathname);
  Object.defineProperty(req, 'watcherAuth', {
    value: Object.freeze({ identity: 'browser', role: null, actor: 'browser', tokenFingerprint: null }),
  });
  Object.defineProperty(req, 'watcherRoute', { value: route });
  const res = {
    statusCode: 200,
    status(value) { this.statusCode = value; return this; },
    json(value) { this.body = value; return this; },
  };
  await createRequestValidation()(req, res, () => handlers.get(pathname)(req, res));
  return { status: res.statusCode, body: res.body };
}

test.before(() => {
  delete process.env.TRADER_TRADING_DB_PATH;
  delete process.env.WATCHER_TRADING_DB;
  process.env.TRADING_DB_PATH = dbPath;
  delete require.cache[require.resolve('../lib/trading-api')];
  const { registerTradingApi } = require('../lib/trading-api');
  const app = {
    get(route, handler) { handlers.set(route, handler); },
    post() {}, put() {}, delete() {},
  };
  registerTradingApi(app, { getDb: () => new Database(dbPath), getStatus: () => ({ running: false }) });
  const { ensureTelegramMessagesTable } = require('../lib/trading-api');
  ensureTelegramMessagesTable();
});

test.after(() => {
  delete require.cache[require.resolve('../lib/trading-api')];
  for (const [key, value] of [
    ['TRADING_DB_PATH', previousPath],
    ['TRADER_TRADING_DB_PATH', previousTraderPath],
    ['WATCHER_TRADING_DB', previousWatcherPath],
  ]) {
    if (value === undefined) {
      delete process.env[key];
    } else {
      process.env[key] = value;
    }
  }
  fs.rmSync(directory, { recursive: true, force: true });
});

for (const table of ['messages', 'briefings']) {
  test(`${table}: real SQLite query returns ordered, gap-free composite cursor pages`, async () => {
    const db = new Database(dbPath);
    db.exec(`DELETE FROM ${table === 'messages' ? 'telegram_messages' : 'briefings'}`);
    db.close();
    const older = insertRows(table, 2, 2);
    const tied = insertRows(table, 5, 1);
    const seen = [];
    let cursor = '';
    for (let page = 0; page < 5; page += 1) {
      const result = await read(table, `?limit=2${cursor}`);
      assert.equal(result.status, 200, JSON.stringify(result.body));
      assert.ok(Array.isArray(result.body));
      seen.push(...result.body.map(row => row.id));
      if (!result.body.length) {
        break;
      }
      const last = result.body.at(-1);
      cursor = `&before_created_at=${encodeURIComponent(last.created_at)}&before_id=${last.id}`;
    }
    assert.deepEqual(seen, [...tied.reverse(), ...older.reverse()]);
    assert.equal(new Set(seen).size, seen.length);
  });

  test(`${table}: hours applies with cursor, default window is 24 hours`, async () => {
    const db = new Database(dbPath);
    db.exec(`DELETE FROM ${table === 'messages' ? 'telegram_messages' : 'briefings'}`);
    db.close();
    insertRows(table, 1, 25);
    insertRows(table, 1, 2);
    const recent = await read(table);
    assert.equal(recent.status, 200);
    assert.equal(recent.body.length, 1);
    const cursor = `?before_created_at=${encodeURIComponent(recent.body[0].created_at)}&before_id=${recent.body[0].id}`;
    assert.deepEqual((await read(table, cursor)).body, []);
    assert.equal((await read(table, `${cursor}&hours=48`)).body.length, 1);
    assert.equal((await read(table, '?hours=48')).body.length, 2);
  });

  test(`${table}: limit defaults to 500 and accepts its upper bound`, async () => {
    const db = new Database(dbPath);
    db.exec(`DELETE FROM ${table === 'messages' ? 'telegram_messages' : 'briefings'}`);
    db.close();
    insertRows(table, 501, 1);
    assert.equal((await read(table)).body.length, 500);
    assert.equal((await read(table, '?limit=500')).body.length, 500);
    assert.equal((await read(table, '?limit=1')).body.length, 1);
  });

  test(`${table}: rejects invalid, duplicate, unknown and incomplete query values`, async () => {
    for (const query of [
      'hours=0', 'hours=169', 'hours=1.5', 'limit=0', 'limit=501',
      'limit=2&limit=3', 'unknown=1', 'before_id=1',
      'before_created_at=2026-09-26%2000%3A00%3A00',
      'before_created_at=bad&before_id=1',
      'before_created_at=2026-09-26%2000%3A00%3A00&before_id=0',
    ]) {
      const result = await read(table, `?${query}`);
      assert.equal(result.status, 400, `${table} ${query}: ${JSON.stringify(result.body)}`);
      assert.equal(result.body.code, 'invalid_query');
    }
  });
}

test('messages: channel filter remains available with paging', async () => {
  const db = new Database(dbPath);
  db.exec('DELETE FROM telegram_messages');
  db.close();
  insertRows('messages', 2, 1, '-100');
  insertRows('messages', 2, 1, '-200');
  const rows = await read('messages', '?channel=-100&limit=1');
  assert.equal(rows.status, 200);
  assert.equal(rows.body.length, 1);
  assert.equal(rows.body[0].channel_id, '-100');
  const last = rows.body[0];
  const next = await read('messages', `?channel=-100&limit=1&before_created_at=${encodeURIComponent(last.created_at)}&before_id=${last.id}`);
  assert.equal(next.status, 200);
  assert.equal(next.body.length, 1);
  assert.equal(next.body[0].channel_id, '-100');
  assert.notEqual(next.body[0].id, last.id);
  const invalid = await read('messages', '?channel=not-a-channel');
  assert.equal(invalid.status, 400);
  assert.equal(invalid.body.code, 'invalid_query');
});
