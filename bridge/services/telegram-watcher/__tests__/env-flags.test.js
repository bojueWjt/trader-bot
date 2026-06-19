const assert = require("node:assert/strict");
const test = require("node:test");

const { isEnabledByDefault } = require("../lib/env-flags");

test("enabled-by-default flag treats empty values as enabled", () => {
  assert.equal(isEnabledByDefault(undefined), true);
  assert.equal(isEnabledByDefault(""), true);
});

test("enabled-by-default flag recognizes disabled tokens", () => {
  assert.equal(isEnabledByDefault("0"), false);
  assert.equal(isEnabledByDefault("false"), false);
  assert.equal(isEnabledByDefault("off"), false);
  assert.equal(isEnabledByDefault("no"), false);
});

test("enabled-by-default flag keeps other values enabled", () => {
  assert.equal(isEnabledByDefault("1"), true);
  assert.equal(isEnabledByDefault("true"), true);
  assert.equal(isEnabledByDefault("yes"), true);
});
