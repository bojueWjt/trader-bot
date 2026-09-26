'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { PAYLOAD, PAYLOAD_SHA256, SECRET_KEY_REGEX } = require('../lib/generated/gateway-routes');

test('committed generated route payload is frozen and complete', () => {
  assert.equal(PAYLOAD._meta.phase_max, 'P3');
  assert.equal(PAYLOAD.routes.filter((row) => row.identity === 'gateway').length, 24);
  assert.equal(PAYLOAD.routes.length, 63);
  assert.equal(PAYLOAD_SHA256.length, 64);
  assert.ok(SECRET_KEY_REGEX.test('API_KEY'));
  assert.ok(Object.isFrozen(PAYLOAD.routes[0].body));
  assert.match(PAYLOAD._meta.yaml_sha256, /^[0-9a-f]{64}$/);
});
