const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
test('D1 legacy Python configuration writes are disabled and reads never migrate', () => {
  const run = spawnSync('python3', ['-B', path.join(__dirname,'db-manager-readonly.test.py')], {encoding:'utf8'});
  assert.equal(run.status,0,run.stdout+run.stderr);
  assert.match(run.stderr,/Ran 3 tests/);
  assert.match(run.stderr,/OK/);
});
