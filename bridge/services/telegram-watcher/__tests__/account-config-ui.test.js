const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

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
