const { requireWatcherContext } = require('./request-validation');
const { digest, sendError } = require('./config-store');
const { PAYLOAD } = require('./generated/gateway-routes');
const clientRefPattern = new RegExp(PAYLOAD.body_fields.client_ref.pattern, 'u');

function failure(status, code, message, details) {
  const error = new Error(message);
  error.status = status;
  error.code = code;
  error.details = details;
  throw error;
}

// A connection must never be held in a transaction across the external effect.
// A crash after the effect and before audit may reapply it; /status is authoritative.
function createReentrantWriter(dbFactory) {
  const pending = new Map();
  function withDb(work) {
    const db = dbFactory();
    try {
      db.pragma('busy_timeout = 5000');
      return work(db);
    } finally {
      db.close();
    }
  }
  function replay(db, key, hash, clientRef, operation) {
    const prior = db.prepare('SELECT * FROM config_audit WHERE idempotency_key = ?').get(key);
    if (!prior) {
      return false;
    }
    if (prior.request_sha256 !== hash) {
      failure(409, 'idempotency_conflict', 'client_ref used with different request', { client_ref: clientRef, operation });
    }
    return { ...JSON.parse(prior.response_json), replay: true };
  }
  return function reentrantHandler(effect) {
    return async (req, res) => {
      let ownKey;
      try {
        const { auth, route } = requireWatcherContext(req);
        if (!route.write || route.write.idempotency !== 'reentrant') {
          failure(500, 'internal_error', 'write route context unavailable');
        }
        if (auth.identity === 'gateway' && auth.role !== 'risk_admin') {
          failure(403, 'identity_forbidden', 'identity forbidden');
        }
        const body = req.body;
        if (!body || typeof body.client_ref !== 'string' || !clientRefPattern.test(body.client_ref)) {
          failure(400, 'invalid_body', 'client_ref required');
        }
        const operation = route.write.operation;
        const key = `${auth.actor}|${operation}|${body.client_ref}`;
        const requestBody = { ...body };
        delete requestBody.client_ref;
        const hash = digest({ method: req.method, path: decodeURIComponent(req.path), body: requestBody });
        const prior = withDb(db => replay(db, key, hash, body.client_ref, operation));
        if (prior) {
          return res.json(prior);
        }
        const running = pending.get(key);
        if (running) {
          if (running.hash !== hash) {
            failure(409, 'idempotency_conflict', 'client_ref used with different request', { client_ref: body.client_ref, operation });
          }
          const payload = await running.promise;
          return res.json({ ...payload, replay: true });
        }
        ownKey = key;
        const promise = (async () => {
          const response = await effect(req);
          return withDb(db => db.transaction(() => {
            const raced = replay(db, key, hash, body.client_ref, operation);
            if (raced) {
              return raced;
            }
            const row = db.prepare('SELECT revision FROM config_revision WHERE id = 1').get();
            if (!row) {
              failure(500, 'internal_error', 'configuration revision unavailable');
            }
            const revision = row.revision;
            const payload = { ...response, revision, replay: false };
            db.prepare('INSERT INTO config_audit (idempotency_key, actor, source, token_fingerprint, operation, request_sha256, status_code, response_json, revision_before, revision_after, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)')
              .run(key, auth.actor, auth.identity, auth.tokenFingerprint, operation, hash, 200, JSON.stringify(payload), revision, revision, new Date().toISOString());
            return payload;
          }).immediate());
        })();
        pending.set(key, { hash, promise });
        return res.json(await promise);
      } catch (error) {
        return sendError(res, error, 'internal_error');
      } finally {
        if (ownKey) {
          pending.delete(ownKey);
        }
      }
    };
  };
}

module.exports = { createReentrantWriter };
