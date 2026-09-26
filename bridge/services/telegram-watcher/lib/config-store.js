const crypto = require("node:crypto");
const { maskSecret } = require("./safe-log");

const SCHEMA_VERSION = "watcher-config-snapshot.v1";
const CLIENT_REF = /^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$/;
const EXECUTION_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const SECRET_KEY = /(?:api[_-]?(?:key|secret|id|hash)|session|password|phone|code|token)/i;
const SECRET_FIELDS = new Set([
  "api_key", "api_secret", "apiKey", "apiSecret", "api_id", "api_hash",
  "apiId", "apiHash", "session", "sessionString", "password", "phoneNumber",
  "phone", "code", "token",
]);
const ACCOUNT_FIELDS = new Set([
  "account_id", "api_key", "api_secret", "account_type", "parent_account_id",
  "execution_account_id", "is_testnet", "default_risk_ratio",
  "risk_capital_multiplier", "risk_capital_addon", "is_enabled",
]);
const ACCOUNT_UPDATE_FIELDS = new Set([...ACCOUNT_FIELDS].filter((field) => field !== "account_id"));
const GATEWAY_ACCOUNT_UPDATE_FIELDS = new Set([
  "is_enabled", "account_type", "parent_account_id", "execution_account_id",
  "default_risk_ratio", "risk_capital_addon",
]);
const CHANNEL_FIELDS = new Set(["channel_id", "target_account_id", "channel_name"]);
const RISK_FIELDS = new Set(["symbol", "risk_ratio"]);
const META_FIELDS = new Set(["client_ref", "expected_revision"]);

function ensureConfigTables(db) {
  db.pragma("busy_timeout = 5000");
  db.exec(
    "CREATE TABLE IF NOT EXISTS config_revision (" +
    "id INTEGER PRIMARY KEY CHECK (id = 1), " +
    "revision INTEGER NOT NULL CHECK (revision >= 0));" +
    "INSERT OR IGNORE INTO config_revision (id, revision) VALUES (1, 0);" +
    "CREATE TABLE IF NOT EXISTS config_audit (" +
    "id INTEGER PRIMARY KEY AUTOINCREMENT, " +
    "idempotency_key TEXT NOT NULL UNIQUE, actor TEXT NOT NULL, " +
    "source TEXT NOT NULL, token_fingerprint TEXT, operation TEXT NOT NULL, " +
    "request_sha256 TEXT NOT NULL, status_code INTEGER NOT NULL, " +
    "response_json TEXT NOT NULL, revision_before INTEGER NOT NULL, " +
    "revision_after INTEGER NOT NULL, created_at TEXT NOT NULL);"
  );
}

function sortObject(value) {
  if (Array.isArray(value)) {
    return value.map(sortObject);
  }
  if (value && typeof value === "object") {
    const result = {};
    for (const key of Object.keys(value).sort()) {
      result[key] = sortObject(value[key]);
    }
    return result;
  }
  return value;
}

function digest(value) {
  return crypto.createHash("sha256")
    .update(JSON.stringify(sortObject(value)), "utf8")
    .digest("hex");
}

function fail(status, code, message, details) {
  const error = new Error(message);
  error.status = status;
  error.code = code;
  error.details = details;
  throw error;
}

function sendError(res, error, defaultCode) {
  const browser = res.req.watcherAuth && res.req.watcherAuth.identity === "browser";
  if (error.code === "SQLITE_BUSY" || error.code === "SQLITE_LOCKED") {
    const response = {
      code: "db_busy", message: "database busy",
      details: { busy_timeout_ms: 5000, retryable: true },
    };
    if (browser) {
      response.error = response.message;
    }
    return res.status(503).json(response);
  }
  if (error.status) {
    const response = { code: error.code, message: error.message };
    if (error.details) {
      response.details = error.details;
      for (const field of ["subaccount_count", "channel_count", "active_order_count"]) {
        if (Object.hasOwn(error.details, field)) {
          response[field] = error.details[field];
        }
      }
    }
    if (browser) {
      response.error = response.message;
    }
    return res.status(error.status).json(response);
  }
  if (["SQLITE_CONSTRAINT_CHECK", "SQLITE_CONSTRAINT_NOTNULL"].includes(error.code)) {
    const response = { code: "validation_failed", message: "configuration validation failed" };
    if (browser) {
      response.error = response.message;
    }
    return res.status(400).json(response);
  }
  if (["SQLITE_CONSTRAINT_UNIQUE", "SQLITE_CONSTRAINT_PRIMARYKEY",
    "SQLITE_CONSTRAINT_FOREIGNKEY"].includes(error.code)) {
    const response = { code: "constraint_conflict", message: "configuration constraint failed" };
    if (browser) {
      response.error = response.message;
    }
    return res.status(409).json(response);
  }
  const response = { code: defaultCode, message: "configuration database unavailable" };
  // Error messages can contain SQL parameters or request values. Log only stable
  // diagnostic fields, never the exception text or request body.
  const errorName = error && typeof error.name === "string" ? error.name : "Error";
  const errorCode = error && typeof error.code === "string" ? error.code : "unknown";
  console.error("[config-store] unexpected error", {
    name: /^[A-Za-z]+Error$/.test(errorName) ? errorName : "Error",
    code: /^SQLITE_[A-Z_]+$/.test(errorCode) ? errorCode : "unknown",
  });
  if (browser) {
    response.error = response.message;
  }
  return res.status(500).json(response);
}

function collectSecretFields(value, found = []) {
  if (!value || typeof value !== "object") {
    return found;
  }
  for (const [key, child] of Object.entries(value)) {
    if (SECRET_FIELDS.has(key) || SECRET_KEY.test(key)) {
      found.push(key);
    }
    collectSecretFields(child, found);
  }
  return found;
}

function validateFields(body, allowed, identity) {
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    fail(400, "invalid_body", "JSON object required");
  }
  if (identity === "gateway") {
    const fields = collectSecretFields(body);
    if (fields.length) {
      fail(400, "secret_field_rejected", "secret field rejected", { fields });
    }
  }
  for (const key of Object.keys(body)) {
    if (!allowed.has(key) && !META_FIELDS.has(key)) {
      fail(400, "invalid_body", "unsupported field");
    }
  }
}

function textValue(value) {
  return typeof value === "string" ? value.trim() : "";
}

function validString(value) {
  return typeof value === "string"
    && value.length > 0
    && !/[\u0000-\u001f\u007f]/u.test(value)
    && !/[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/u.test(value);
}

function numberValue(value, name, low, inclusiveLow) {
  if (value === null || value === "" || typeof value === "boolean") {
    fail(400, "validation_failed", name + " is invalid", { field: name, rule: "range" });
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)
    || (numeric > 0.1 && name !== "risk_capital_addon" && name !== "risk_capital_multiplier")) {
    fail(400, "validation_failed", name + " is invalid", { field: name, rule: "range" });
  }
  if (inclusiveLow ? numeric < low : numeric <= low) {
    fail(400, "validation_failed", name + " is invalid", { field: name, rule: "range" });
  }
  return numeric;
}

function booleanValue(value, name) {
  if (value === true || value === 1 || value === "1" || value === "true") {
    return 1;
  }
  if (value === false || value === 0 || value === "0" || value === "false") {
    return 0;
  }
  fail(400, "invalid_body", name + " is invalid");
}

function cleanAccount(row, identity) {
  const result = { ...row };
  if (identity === "gateway") {
    delete result.api_key;
    delete result.api_secret;
  } else {
    result.api_key = maskSecret(row.api_key);
    result.api_secret = maskSecret(row.api_secret);
  }
  return result;
}

function accountRow(db, key) {
  return db.prepare("SELECT * FROM account_configs WHERE account_id = ?").get(key);
}

function resourceRow(db, resource, key, identity) {
  if (resource === "account") {
    const row = accountRow(db, key);
    return row ? cleanAccount(row, identity) : null;
  }
  if (resource === "channel") {
    return db.prepare("SELECT channel_id, target_account_id, channel_name FROM channel_routing WHERE channel_id = ?").get(key) || null;
  }
  return db.prepare("SELECT symbol, risk_ratio FROM symbol_risk_configs WHERE symbol = ?").get(key) || null;
}

function validateHierarchy(db, row) {
  if (row.account_type !== "main" && row.account_type !== "subaccount") {
    fail(400, "invalid_body", "account_type must be main or subaccount");
  }
  if (row.account_type === "main") {
    if (row.parent_account_id) {
      fail(400, "invalid_body", "main account cannot have parent");
    }
  } else {
    const parentId = textValue(row.parent_account_id);
    if (!parentId || parentId === row.account_id) {
      fail(400, "invalid_body", "subaccount requires another main account");
    }
    const parent = accountRow(db, parentId);
    if (!parent || parent.account_type !== "main" || parent.is_enabled !== 1) {
      fail(400, "invalid_body", "parent must be an enabled main account");
    }
    if (parent.is_testnet !== row.is_testnet) {
      fail(400, "invalid_body", "subaccount environment must match parent");
    }
  }
  const child = db.prepare(
    "SELECT account_id, is_testnet FROM account_configs " +
    "WHERE parent_account_id = ? AND account_type = 'subaccount' LIMIT 1"
  ).get(row.account_id);
  if (child && (row.account_type !== "main" || child.is_testnet !== row.is_testnet)) {
    fail(409, "constraint_conflict", "move or update child accounts first");
  }
}

function validateAccountInput(db, input, current, identity) {
  const row = current ? { ...current } : {
    account_type: "main", parent_account_id: "",
    default_risk_ratio: 0.01, is_testnet: 0,
    risk_capital_multiplier: 1, is_enabled: 1,
  };
  let fields = current ? ACCOUNT_UPDATE_FIELDS : ACCOUNT_FIELDS;
  if (identity === "gateway") {
    fields = GATEWAY_ACCOUNT_UPDATE_FIELDS;
  }
  validateFields(input, fields, identity);
  if (current && Object.hasOwn(input, "account_id")) {
    fail(400, "invalid_body", "account_id is immutable");
  }
  if (!current) {
    if (!validString(input.account_id) || input.account_id.length > 128) {
      fail(400, "invalid_body", "account_id is invalid");
    }
    row.account_id = input.account_id;
    if (identity !== "browser") {
      fail(403, "identity_forbidden", "account creation requires browser");
    }
  }
  for (const field of fields) {
    if (Object.hasOwn(input, field)) {
      row[field] = input[field];
    }
  }
  if (!current && !Object.hasOwn(input, "execution_account_id")) {
    row.execution_account_id = row.account_id;
  }
  if (!current && !Object.hasOwn(input, "risk_capital_addon")) {
    fail(400, "invalid_body", "risk_capital_addon is required");
  }
  if ((!current || Object.hasOwn(input, "execution_account_id"))
    && (!validString(row.execution_account_id) || !EXECUTION_ID.test(row.execution_account_id))) {
    fail(400, "invalid_body", "execution_account_id is invalid");
  }
  for (const field of ["api_key", "api_secret"]) {
    if (!current && !textValue(row[field])) {
      fail(400, "invalid_body", "account credentials required");
    }
    if (current && Object.hasOwn(input, field)) {
      if (typeof input[field] !== "string") {
        fail(400, "invalid_body", field + " must be a string");
      }
      const supplied = textValue(input[field]);
      const masked = maskSecret(current[field]);
      if (supplied === masked || /^\*{4}$/.test(supplied) || /^.{4}\.\.\..{4}$/.test(supplied)) {
        fail(400, "masked_value_rejected", "masked credential rejected");
      }
      if (!supplied) {
        row[field] = current[field];
      }
    }
  }
  if (Object.hasOwn(input, "default_risk_ratio") || !current) {
    row.default_risk_ratio = numberValue(row.default_risk_ratio, "default_risk_ratio", 0, false);
  }
  if (Object.hasOwn(input, "risk_capital_addon") || !current) {
    row.risk_capital_addon = numberValue(row.risk_capital_addon, "risk_capital_addon", 0, true);
  }
  if (Object.hasOwn(input, "risk_capital_multiplier") || !current) {
    row.risk_capital_multiplier = numberValue(row.risk_capital_multiplier, "risk_capital_multiplier", 0, false);
  }
  for (const field of ["is_testnet", "is_enabled"]) {
    if (Object.hasOwn(input, field)) {
      row[field] = booleanValue(input[field], field);
    }
  }
  if (Object.hasOwn(input, "account_type") && !["main", "subaccount"].includes(row.account_type)) {
    fail(400, "invalid_body", "account_type is invalid");
  }
  if (Object.hasOwn(input, "parent_account_id") && typeof row.parent_account_id !== "string") {
    fail(400, "invalid_body", "parent_account_id is invalid");
  }
  if (!current || ["account_type", "parent_account_id", "is_testnet"].some((field) => Object.hasOwn(input, field))) {
    validateHierarchy(db, row);
  }
  if (!current || Object.hasOwn(input, "execution_account_id")) {
    const duplicate = db.prepare("SELECT account_id FROM account_configs WHERE execution_account_id = ?").get(row.execution_account_id);
    if (duplicate && duplicate.account_id !== row.account_id) {
      fail(409, "constraint_conflict", "execution account already exists");
    }
  }
  return row;
}

function writeBusiness(db, resource, action, key, body, identity) {
  if (resource === "account") {
    if (action === "create") {
      if (accountRow(db, body.account_id)) {
        fail(409, "constraint_conflict", "account already exists");
      }
      const row = validateAccountInput(db, body, null, identity);
      db.prepare("INSERT INTO account_configs (account_id, api_key, api_secret, default_risk_ratio, " +
        "is_testnet, account_type, parent_account_id, execution_account_id, " +
        "risk_capital_multiplier, risk_capital_addon, is_enabled) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
        .run(row.account_id, row.api_key, row.api_secret, row.default_risk_ratio, row.is_testnet,
          row.account_type, row.parent_account_id, row.execution_account_id,
          row.risk_capital_multiplier, row.risk_capital_addon, row.is_enabled);
      return { ok: true, account_id: row.account_id };
    }
    const current = accountRow(db, key);
    if (!current) {
      fail(404, "not_found", "account not found");
    }
    if (action === "update") {
      const row = validateAccountInput(db, body, current, identity);
      db.prepare("UPDATE account_configs SET api_key = ?, api_secret = ?, default_risk_ratio = ?, " +
        "is_testnet = ?, account_type = ?, parent_account_id = ?, execution_account_id = ?, " +
        "risk_capital_multiplier = ?, risk_capital_addon = ?, is_enabled = ? WHERE account_id = ?")
        .run(row.api_key, row.api_secret, row.default_risk_ratio, row.is_testnet,
          row.account_type, row.parent_account_id, row.execution_account_id,
          row.risk_capital_multiplier, row.risk_capital_addon, row.is_enabled, key);
      return { ok: true, account_id: key };
    }
    const childCount = db.prepare("SELECT COUNT(*) AS n FROM account_configs WHERE parent_account_id = ? AND account_type = 'subaccount'").get(key).n;
    const channelCount = db.prepare("SELECT COUNT(*) AS n FROM channel_routing WHERE target_account_id = ?").get(key).n;
    const activeCount = db.prepare("SELECT COUNT(*) AS n FROM active_orders WHERE account_id = ? AND status IN ('PENDING','OPEN','PARTIAL_CLOSED')").get(key).n;
    if (childCount) {
      fail(409, "constraint_conflict", "Account has subaccounts", { subaccount_count: childCount });
    }
    if (channelCount) {
      fail(409, "constraint_conflict", "Account is used by channel routing", { channel_count: channelCount });
    }
    if (activeCount) {
      fail(409, "constraint_conflict", "Account has active orders", { active_order_count: activeCount });
    }
    db.prepare("DELETE FROM account_configs WHERE account_id = ?").run(key);
    return { ok: true };
  }
  if (resource === "channel") {
    validateFields(body, action === "upsert" ? CHANNEL_FIELDS : new Set(), identity);
    if (action === "upsert") {
      const channelId = textValue(body.channel_id);
      const targetId = textValue(body.target_account_id);
      if (!validString(channelId) || !validString(targetId)) {
        fail(400, "invalid_body", "channel_id and target_account_id required");
      }
      const account = accountRow(db, targetId);
      if (!account || account.is_enabled !== 1) {
        fail(400, "invalid_body", "target account missing or disabled");
      }
      if (body.channel_name !== undefined && typeof body.channel_name !== "string") {
        fail(400, "invalid_body", "channel_name is invalid");
      }
      db.prepare("INSERT INTO channel_routing (channel_id, target_account_id, channel_name) VALUES (?, ?, ?) " +
        "ON CONFLICT(channel_id) DO UPDATE SET target_account_id=excluded.target_account_id, channel_name=excluded.channel_name")
        .run(channelId, targetId, body.channel_name || "");
      return { ok: true, channel_id: channelId, target_account_id: targetId };
    }
    if (!db.prepare("DELETE FROM channel_routing WHERE channel_id = ?").run(key).changes) {
      fail(404, "not_found", "channel routing not found");
    }
    return { ok: true };
  }
  validateFields(body, action === "upsert" ? RISK_FIELDS : new Set(), identity);
  if (action === "upsert") {
    const symbol = textValue(body.symbol);
    if (!validString(symbol) || body.risk_ratio === undefined) {
      fail(400, "invalid_body", "symbol and risk_ratio required");
    }
    const ratio = numberValue(body.risk_ratio, "risk_ratio", 0, false);
    db.prepare("INSERT INTO symbol_risk_configs (symbol, risk_ratio) VALUES (?, ?) " +
      "ON CONFLICT(symbol) DO UPDATE SET risk_ratio=excluded.risk_ratio").run(symbol, ratio);
    return { ok: true, symbol, risk_ratio: ratio };
  }
  if (!db.prepare("DELETE FROM symbol_risk_configs WHERE symbol = ?").run(key).changes) {
    fail(404, "not_found", "risk config not found");
  }
  return { ok: true };
}

function transactWrite(db, req, resource, action, key) {
  const body = req.body;
  const identity = req.watcherAuth && req.watcherAuth.identity;
  if (identity !== "gateway" && identity !== "browser") {
    fail(identity ? 403 : 401, identity ? "identity_forbidden" : "unauthenticated",
      "configuration identity required");
  }
  if (identity === "gateway" && req.watcherAuth.role !== "risk_admin") {
    fail(403, "insufficient_scope", "risk_admin required");
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    fail(400, "invalid_body", "JSON object required");
  }
  if (identity === "gateway") {
    const secretFields = collectSecretFields(body);
    if (secretFields.length) {
      fail(400, "secret_field_rejected", "secret field rejected", { fields: secretFields });
    }
  }
  if (!CLIENT_REF.test(body.client_ref || "") || !Number.isSafeInteger(body.expected_revision) || body.expected_revision < 0) {
    fail(400, "invalid_body", "client_ref and expected_revision required");
  }
  if (action === "delete" && Object.keys(body).some((name) => !META_FIELDS.has(name))) {
    fail(400, "invalid_body", "unexpected delete field");
  }
  const operation = resource + "." + action;
  const auth = req.watcherAuth;
  const actor = identity === "gateway" ? auth.actor : "browser";
  if (!actor || typeof actor !== "string") {
    fail(400, "invalid_body", "actor required");
  }
  const idempotencyKey = actor + "|" + operation + "|" + body.client_ref;
  const requestBody = { ...body };
  delete requestBody.client_ref;
  let decodedPath;
  try {
    decodedPath = decodeURIComponent(req.path);
  } catch (error) {
    fail(400, "invalid_body", "invalid request path");
  }
  const requestHash = digest({ method: req.method, path: decodedPath, body: requestBody });
  const transaction = db.transaction(() => {
    const prior = db.prepare("SELECT request_sha256, status_code, response_json FROM config_audit WHERE idempotency_key = ?").get(idempotencyKey);
    if (prior) {
      if (prior.request_sha256 !== requestHash) {
        fail(409, "idempotency_conflict", "client_ref used with different request",
          { client_ref: body.client_ref, operation });
      }
      return { status: prior.status_code, body: { ...JSON.parse(prior.response_json), replay: true } };
    }
    const row = db.prepare("SELECT revision FROM config_revision WHERE id = 1").get();
    if (!row) {
      fail(500, "snapshot_unreadable", "configuration revision missing");
    }
    const revision = row.revision;
    if (body.expected_revision !== revision) {
      const currentKey = key || body.account_id || body.channel_id || body.symbol;
      fail(409, "revision_conflict", "config revision changed", {
        expected_revision: body.expected_revision, current_revision: revision,
        resource, key: currentKey,
        current: resourceRow(db, resource, currentKey, identity),
      });
    }
    const response = writeBusiness(db, resource, action, key, body, identity);
    const next = revision + 1;
    db.prepare("UPDATE config_revision SET revision = ? WHERE id = 1").run(next);
    const payload = { ...response, revision: next, replay: false };
    db.prepare("INSERT INTO config_audit (idempotency_key, actor, source, token_fingerprint, " +
      "operation, request_sha256, status_code, response_json, revision_before, revision_after, created_at) " +
      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
      .run(idempotencyKey, actor, identity,
        identity === "gateway" ? (auth.tokenFingerprint || null) : null,
        operation, requestHash, 200, JSON.stringify(payload), revision, next, new Date().toISOString());
    return { status: 200, body: payload };
  });
  return transaction.immediate();
}

function withDb(dbFactory, res, work, defaultCode = "internal_error") {
  let db;
  try {
    db = dbFactory();
    db.pragma("busy_timeout = 5000");
    work(db);
  } catch (error) {
    sendError(res, error, defaultCode);
  } finally {
    if (db) {
      db.close();
    }
  }
}

function readList(db, resource, identity) {
  if (resource === "account") {
    const rows = db.prepare(
      "SELECT account.*, " +
      "(SELECT COUNT(*) FROM channel_routing route WHERE route.target_account_id=account.account_id) AS channel_count, " +
      "(SELECT COUNT(*) FROM account_configs child WHERE child.account_type='subaccount' AND child.parent_account_id=account.account_id) AS subaccount_count " +
      "FROM account_configs account ORDER BY CASE WHEN account.account_type='main' THEN account.account_id ELSE account.parent_account_id END, " +
      "CASE account.account_type WHEN 'main' THEN 0 ELSE 1 END, account.account_id"
    ).all();
    return rows.map((row) => cleanAccount(row, identity));
  }
  if (resource === "channel") {
    return db.prepare(
      "SELECT route.channel_id, route.target_account_id, route.channel_name, account.account_type AS target_account_type, " +
      "account.parent_account_id, account.execution_account_id, account.is_enabled AS target_is_enabled " +
      "FROM channel_routing route LEFT JOIN account_configs account ON account.account_id=route.target_account_id " +
      "ORDER BY route.channel_name, route.channel_id"
    ).all();
  }
  return db.prepare("SELECT symbol, risk_ratio FROM symbol_risk_configs ORDER BY symbol").all();
}

function snapshotInvalid(violations) {
  fail(500, "snapshot_invalid", "configuration snapshot invalid", { violations });
}

function makeSnapshot(db) {
  const revisionRow = db.prepare("SELECT revision FROM config_revision WHERE id = 1").get();
  if (!revisionRow || !Number.isSafeInteger(revisionRow.revision) || revisionRow.revision < 0) {
    fail(500, "snapshot_unreadable", "configuration revision unavailable");
  }
  const columns = {
    account_configs: ["account_id", "account_type", "parent_account_id", "execution_account_id", "is_enabled", "risk_capital_addon", "default_risk_ratio"],
    channel_routing: ["channel_id", "target_account_id"],
    symbol_risk_configs: ["symbol", "risk_ratio"],
  };
  const missing = [];
  for (const [table, required] of Object.entries(columns)) {
    const present = new Set(db.prepare("PRAGMA table_info(" + table + ")").all().map((row) => row.name));
    if (present.size === 0) {
      fail(500, "snapshot_unreadable", "configuration table unavailable");
    }
    for (const name of required) {
      if (!present.has(name)) {
        missing.push({ rule: "missing_field", table, key: name });
      }
    }
  }
  if (missing.length) {
    snapshotInvalid(missing);
  }
  const accountsRaw = db.prepare(
    "SELECT account_id, account_type, parent_account_id, execution_account_id, is_enabled, " +
    "risk_capital_addon, default_risk_ratio FROM account_configs ORDER BY account_id COLLATE BINARY"
  ).all();
  const channelsRaw = db.prepare(
    "SELECT channel_id, target_account_id FROM channel_routing ORDER BY channel_id COLLATE BINARY"
  ).all();
  const risksRaw = db.prepare(
    "SELECT symbol, risk_ratio FROM symbol_risk_configs ORDER BY symbol COLLATE BINARY"
  ).all();
  const violations = [];
  const add = (rule, table, key) => violations.push({ rule, table, key: validString(key) ? key : "" });
  const ids = new Set();
  const executionIds = new Set();
  for (const row of accountsRaw) {
    const key = row.account_id;
    if (!validString(key) || !validString(row.execution_account_id)
      || (row.parent_account_id !== "" && row.parent_account_id !== null && !validString(row.parent_account_id))) {
      add("invalid_string", "account_configs", key);
    }
    if (row.account_type !== "main" && row.account_type !== "subaccount") {
      add("invalid_enum", "account_configs", key);
    }
    if (row.is_enabled !== 0 && row.is_enabled !== 1) {
      add("invalid_enum", "account_configs", key);
    }
    if (row.account_id === null || row.account_type === null || row.parent_account_id === null
      || row.execution_account_id === null || row.is_enabled === null || row.risk_capital_addon === null) {
      add("missing_field", "account_configs", key);
    }
    if (executionIds.has(row.execution_account_id)) {
      add("duplicate_execution_account", "account_configs", key);
    }
    ids.add(key);
    executionIds.add(row.execution_account_id);
    if (typeof row.risk_capital_addon !== "number" || !Number.isFinite(row.risk_capital_addon)
      || row.risk_capital_addon < 0) {
      add("invalid_number", "account_configs", key);
    }
    if (row.default_risk_ratio !== null
      && (typeof row.default_risk_ratio !== "number" || !Number.isFinite(row.default_risk_ratio)
        || row.default_risk_ratio <= 0 || row.default_risk_ratio > 0.1)) {
      add("invalid_number", "account_configs", key);
    }
  }
  const byId = new Map(accountsRaw.map((row) => [row.account_id, row]));
  for (const row of accountsRaw) {
    const parent = byId.get(row.parent_account_id);
    if (row.account_type === "main" && row.parent_account_id) {
      add("invalid_parent", "account_configs", row.account_id);
    }
    if (row.account_type === "subaccount"
      && (!parent || parent.account_type !== "main" || row.parent_account_id === row.account_id)) {
      add("invalid_parent", "account_configs", row.account_id);
    }
  }
  for (const row of channelsRaw) {
    if (!validString(row.channel_id) || (row.target_account_id !== null && !validString(row.target_account_id))) {
      add("invalid_string", "channel_routing", row.channel_id);
    }
    if (row.channel_id === null || row.target_account_id === null) {
      add("missing_field", "channel_routing", row.channel_id);
    } else if (!ids.has(row.target_account_id)) {
      add("dangling_route", "channel_routing", row.channel_id);
    }
  }
  for (const row of risksRaw) {
    if (!validString(row.symbol)) {
      add("invalid_string", "symbol_risk_configs", row.symbol);
    }
    if (row.symbol === null || row.risk_ratio === null) {
      add("missing_field", "symbol_risk_configs", row.symbol);
    } else if (typeof row.risk_ratio !== "number" || !Number.isFinite(row.risk_ratio)
      || row.risk_ratio <= 0 || row.risk_ratio > 0.1) {
      add("invalid_number", "symbol_risk_configs", row.symbol);
    }
  }
  if (violations.length) {
    snapshotInvalid(violations);
  }
  const accounts = accountsRaw.map((row) => ({
    account_id: row.account_id,
    kind: row.account_type === "main" ? "main" : "sub",
    parent_account_id: row.account_type === "main" ? null : row.parent_account_id,
    execution_account_id: row.execution_account_id,
    enabled: row.is_enabled === 1,
    risk_capital_addon: String(Number(row.risk_capital_addon)),
    default_risk: row.default_risk_ratio === null ? null : String(Number(row.default_risk_ratio)),
  }));
  const channels = channelsRaw.map((row) => ({
    channel_id: row.channel_id, target_account_id: row.target_account_id,
  }));
  const risks = risksRaw.map((row) => ({
    symbol: row.symbol, risk_ratio: String(Number(row.risk_ratio)),
  }));
  const content = { accounts, channels, risks, schema_version: SCHEMA_VERSION };
  return {
    schema_version: SCHEMA_VERSION,
    revision: revisionRow.revision,
    content_sha256: digest(content),
    generated_at: new Date().toISOString(),
    accounts, channels, risks,
  };
}

function registerConfigRoutes(app, dbFactory) {
  app.get("/api/trading/config-snapshot", (req, res) => {
    const identity = req.watcherAuth && req.watcherAuth.identity;
    if (identity !== "snapshot") {
      return res.status(identity ? 403 : 401).json({
        code: identity ? "identity_forbidden" : "unauthenticated",
        message: "snapshot identity required",
      });
    }
    if (Object.keys(req.query).length || (req.body && Object.keys(req.body).length)) {
      return res.status(400).json({ code: "invalid_body", message: "snapshot has no parameters" });
    }
    withDb(dbFactory, res, (db) => {
      const snapshot = db.transaction(() => makeSnapshot(db))();
      res.json(snapshot);
    }, "snapshot_unreadable");
  });
  const resources = [
    { resource: "account", collection: "/api/trading/accounts", item: "/api/trading/accounts/:id" },
    { resource: "channel", collection: "/api/trading/channels", item: "/api/trading/channels/:id" },
    { resource: "risk", collection: "/api/trading/risks", item: "/api/trading/risks/:symbol" },
  ];
  for (const { resource, collection, item } of resources) {
    app.get(collection, (req, res) => withDb(dbFactory, res, (db) => {
      const result = db.transaction(() => {
        const revision = db.prepare("SELECT revision FROM config_revision WHERE id = 1").get();
        if (!revision) {
          fail(500, "snapshot_unreadable", "configuration revision unavailable");
        }
        return { rows: readList(db, resource, req.watcherAuth && req.watcherAuth.identity), revision: revision.revision };
      })();
      res.set("X-Config-Revision", String(result.revision)).json(result.rows);
    }));
    const createAction = resource === "account" ? "create" : "upsert";
    app.post(collection, (req, res) => withDb(dbFactory, res, (db) => {
      const result = transactWrite(db, req, resource, createAction, null);
      res.status(result.status).json(result.body);
    }));
    if (resource === "account") {
      app.put(item, (req, res) => withDb(dbFactory, res, (db) => {
        const result = transactWrite(db, req, resource, "update", req.params.id);
        res.status(result.status).json(result.body);
      }));
    }
    app.delete(item, (req, res) => withDb(dbFactory, res, (db) => {
      const key = resource === "risk" ? req.params.symbol : req.params.id;
      const result = transactWrite(db, req, resource, "delete", key);
      res.status(result.status).json(result.body);
    }));
  }
}

module.exports = {
  ensureConfigTables,
  registerConfigRoutes,
  __test: { makeSnapshot, digest, transactWrite },
};
