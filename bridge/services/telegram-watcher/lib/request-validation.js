const { PAYLOAD, SECRET_KEY_REGEX } = require('./generated/gateway-routes');
const secretFields = new Set(PAYLOAD.secret_fields);
const routeRows = new Set(PAYLOAD.routes);
const LIMIT = PAYLOAD.budgets.config.max_request_body_bytes;

function collectSecretFields(value) {
  const found = [];
  const pending = [value];
  while (pending.length) {
    const current = pending.pop();
    if (!current || typeof current !== 'object') {
      continue;
    }
    for (const [key, child] of Object.entries(current)) {
      if (/[^\x00-\x7f]/u.test(key) || secretFields.has(key) || SECRET_KEY_REGEX.test(key)) {
        found.push(key);
      }
      if (child && typeof child === 'object') {
        pending.push(child);
      }
    }
  }
  return found;
}

function requireWatcherContext(req) {
  const { watcherAuth: auth, watcherRoute: route } = req;
  const keys = ['identity', 'role', 'actor', 'tokenFingerprint'];
  let valid = auth && routeRows.has(route) && auth.identity === route.identity;
  if (valid) {
    valid = Object.keys(auth).length === keys.length && keys.every(key => Object.hasOwn(auth, key));
    if (auth.identity === 'gateway') {
      valid = valid && PAYLOAD.roles.all_readers.includes(auth.role) && auth.actor === `app:${auth.role}` && typeof auth.tokenFingerprint === 'string' && /^[0-9a-f]{12}$/u.test(auth.tokenFingerprint);
    } else {
      valid = valid && auth.role === null && auth.actor === auth.identity && auth.tokenFingerprint === null;
    }
  }
  if (!valid) {
    const error = new Error('watcher request context unavailable');
    error.status = 500;
    error.code = 'internal_error';
    throw error;
  }
  return { auth, route };
}

function reject(res, req, status, code, details) {
  const messages = { internal_error: 'watcher request context unavailable', invalid_query: 'invalid query', invalid_body: 'invalid request body', secret_field_rejected: 'secret field rejected', payload_too_large: 'request body too large' };
  const payload = { code, message: messages[code] };
  if (details) {
    payload.details = details;
  }
  if (req.watcherAuth && req.watcherAuth.identity === 'browser') {
    payload.error = payload.message;
  }
  return res.status(status).json(payload);
}

function validValue(value, definition, identity) {
  if (!definition) {
    return false;
  }
  const type = definition.type;
  const numberLike = typeof value === 'number' || (typeof value === 'string' && value.trim() !== '');
  const numeric = numberLike ? Number(value) : NaN;
  const types = {
    string: typeof value === 'string',
    integer: Number.isSafeInteger(value),
    number: typeof value === 'number' && Number.isFinite(value),
    boolean: typeof value === 'boolean',
    array: Array.isArray(value),
    string_or_integer: typeof value === 'string' || Number.isSafeInteger(value),
    boolean_like: [true, false, 0, 1, '0', '1', 'true', 'false'].includes(value),
    integer_like: Number.isSafeInteger(numeric),
    number_like: Number.isFinite(numeric),
  };
  if (!types[type] || (identity === 'gateway' && definition.browser_only)) {
    return false;
  }
  const allowed = identity === 'gateway' && definition.gateway_enum ? definition.gateway_enum : definition.enum;
  if (allowed && !allowed.includes(value)) {
    return false;
  }
  const pattern = identity === 'gateway' && definition.gateway_pattern ? definition.gateway_pattern : definition.pattern;
  if (pattern && (typeof value !== 'string' || !new RegExp(pattern, 'u' + (definition.flags || '')).test(value))) {
    return false;
  }
  if (typeof value === 'string') {
    const length = [...value].length;
    if ((definition.min_length !== undefined && length < definition.min_length) || (definition.max_length !== undefined && length > definition.max_length)) {
      return false;
    }
  }
  if (typeof value === 'number') {
    if ((definition.min !== undefined && value < definition.min) || (definition.max !== undefined && value > definition.max) || (definition.exclusive_min !== undefined && value <= definition.exclusive_min)) {
      return false;
    }
  }
  if (Array.isArray(value)) {
    if ((definition.max_items !== undefined && value.length > definition.max_items) || (definition.unique && new Set(value).size !== value.length)) {
      return false;
    }
    return value.every(item => validValue(item, PAYLOAD.body_fields[definition.items], identity));
  }
  return true;
}

function validateQuery(req, route) {
  const index = req.originalUrl.indexOf('?');
  const params = new URLSearchParams(index < 0 ? '' : req.originalUrl.slice(index + 1));
  for (const [key, text] of params) {
    const definition = PAYLOAD.query_params[key];
    if (!route.query.includes(key) || params.getAll(key).length !== 1 || !definition) {
      return false;
    }
    let value = text;
    if (definition.type === 'integer') {
      if (!/^[0-9]+$/u.test(text)) {
        return false;
      }
      value = Number(text);
    }
    if (!validValue(value, definition, req.watcherAuth.identity) || (definition.requires && !params.has(definition.requires))) {
      return false;
    }
  }
  return true;
}

function createRequestValidation() {
  return async function watcherRequestValidation(req, res, next) {
    let context;
    try {
      context = requireWatcherContext(req);
    } catch {
      return reject(res, req, 500, 'internal_error');
    }
    const { route, auth } = context;
    if (!validateQuery(req, route)) {
      return reject(res, req, 400, 'invalid_query');
    }
    const readOnly = req.method === 'GET' || req.method === 'HEAD';
    const length = Number(req.headers['content-length'] || 0);
    if (readOnly && (length > 0 || req.headers['transfer-encoding'])) {
      return reject(res, req, 400, 'invalid_body');
    }
    if (length > LIMIT) {
      return reject(res, req, 413, 'payload_too_large');
    }
    let size = 0;
    const chunks = [];
    // Destroying the iterator on early return would also destroy the HTTP socket
    // before its structured error can be delivered. Stop consumption without it.
    try {
      for await (const chunk of req.iterator({ destroyOnReturn: false })) {
        const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
        size += buffer.length;
        if (readOnly && size) {
          return reject(res, req, 400, 'invalid_body');
        }
        if (size > LIMIT) {
          return reject(res, req, 413, 'payload_too_large');
        }
        chunks.push(buffer);
      }
    } catch {
      return reject(res, req, 400, 'invalid_body');
    }
    if (readOnly) {
      return next();
    }
    const contentType = String(req.headers['content-type'] || '').split(';')[0].trim().toLowerCase();
    if (contentType !== 'application/json' || (req.headers['content-encoding'] && req.headers['content-encoding'] !== 'identity')) {
      return reject(res, req, 400, 'invalid_body');
    }
    let body;
    try {
      body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    } catch {
      return reject(res, req, 400, 'invalid_body');
    }
    if (!body || typeof body !== 'object' || Array.isArray(body)) {
      return reject(res, req, 400, 'invalid_body');
    }
    if (auth.identity === 'gateway') {
      const fields = collectSecretFields(body);
      if (fields.length) {
        return reject(res, req, 400, 'secret_field_rejected', { fields });
      }
    }
    const unknown = Object.keys(body).filter(key => !route.body.allow.includes(key) || route.body.deny.includes(key));
    if (unknown.length) {
      return reject(res, req, 400, 'invalid_body', { unknown_fields: unknown });
    }
    const missing = route.body.required.filter(key => !Object.hasOwn(body, key));
    if (missing.length) {
      return reject(res, req, 400, 'invalid_body', { missing_fields: missing });
    }
    for (const [key, value] of Object.entries(body)) {
      const overrides = route.body.field_overrides || {};
      const definitionKey = overrides[key] || key;
      if (!validValue(value, PAYLOAD.body_fields[definitionKey], auth.identity)) {
        return reject(res, req, 400, 'invalid_body', { field: key, rule: 'type_or_format' });
      }
    }
    req.body = body;
    return next();
  };
}

module.exports = { createRequestValidation, requireWatcherContext, collectSecretFields };
