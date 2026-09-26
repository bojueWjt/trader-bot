'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { PAYLOAD, PAYLOAD_SHA256, SECRET_KEY_REGEX } = require('../lib/generated/gateway-routes');

test('committed generated route payload is frozen and complete', () => {
  const source = path.resolve(__dirname, '../../../../contracts/watcher-gateway-routes.yaml');
  const script = 'import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))["routes"]))';
  const run = spawnSync('python3', ['-c', script, source], { encoding: 'utf8' });
  assert.equal(run.status, 0, run.stderr);
  const sourceRows = JSON.parse(run.stdout);
  const phases = [...new Set(sourceRows.map((row) => row.phase))].sort();
  const phaseMax = PAYLOAD._meta.phase_max;
  assert.equal(phaseMax, 'P2');
  const expected = sourceRows.filter((row) => row.identity !== 'gateway' || phases.indexOf(row.phase) <= phases.indexOf(phaseMax));
  assert.deepEqual(new Set(PAYLOAD.routes.map((row) => row.id)), new Set(expected.map((row) => row.id)));
  assert.equal(PAYLOAD.routes.length, expected.length);
  assert.equal(PAYLOAD_SHA256.length, 64);
  assert.ok(SECRET_KEY_REGEX.test('API_KEY'));
  assert.ok(Object.isFrozen(PAYLOAD.routes[0].body));
  assert.match(PAYLOAD._meta.yaml_sha256, /^[0-9a-f]{64}$/);
});

test('generated JS rejects a tampered payload at load time', () => {
  const original = fs.readFileSync(path.resolve(__dirname, '../lib/generated/gateway-routes.js'), 'utf8');
  const altered = original.replace('const _TEXT = "', 'const _TEXT = "x');
  assert.notEqual(altered, original);
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'watcher-routes-'));
  try {
    const file = path.join(dir, 'gateway-routes.js');
    fs.writeFileSync(file, altered);
    assert.throws(() => require(file), /digest mismatch/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
