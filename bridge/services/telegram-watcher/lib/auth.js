const crypto = require("node:crypto");
const { PAYLOAD } = require("./generated/gateway-routes");
const { neverAllowed } = require("./generated/watcher-routes");
const { routes, path_params: pathParams, actor_headers: actorHeaders } = PAYLOAD;

// Python str.strip(), including the C0 separators omitted by JavaScript trim().
function stripToken(value) {
  return value.replace(/^[\t\n\v\f\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\t\n\v\f\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/gu, "");
}

const TOKEN_ENV = {
  gateway: "WATCHER_GATEWAY_TOKEN",
  snapshot: "WATCHER_SNAPSHOT_TOKEN",
  browser: "WATCHER_BROWSER_PROXY_TOKEN",
};

function tokenCatalog(env) {
  const catalog = {};
  const seen = new Set();
  for (const [identity, name] of Object.entries(TOKEN_ENV)) {
    const current = String(env[name] || "");
    const previous = String(env[`${name}_PREVIOUS`] || "");
    if (!/^[\x21-\x7E]{32,}$/u.test(current)) {
      throw new Error(`${name} must contain at least 32 printable ASCII bytes without whitespace`);
    }
    const values = [current];
    if (previous) {
      if (!/^[\x21-\x7E]{32,}$/u.test(previous)) {
        throw new Error(`${name}_PREVIOUS must contain at least 32 printable ASCII bytes without whitespace`);
      }
      values.push(previous);
    }
    for (const value of values) {
      if (seen.has(value)) {
        throw new Error("WATCHER_* token values must be distinct");
      }
      seen.add(value);
    }
    catalog[identity] = values.map((value) => crypto.createHash("sha256").update(value, "utf8").digest());
  }
  return catalog;
}

function matchesToken(value, digests) {
  if (!value || digests.length === 0) {
    return false;
  }
  const candidate = crypto.createHash("sha256").update(value, "utf8").digest();
  let matched = 0;
  for (const digest of digests) {
    matched |= Number(crypto.timingSafeEqual(candidate, digest));
  }
  return matched !== 0;
}

function rawValues(req, name) {
  const values = [];
  for (let index = 0; index < req.rawHeaders.length; index += 2) {
    if (req.rawHeaders[index].toLowerCase() === name) {
      values.push(req.rawHeaders[index + 1]);
    }
  }
  return values;
}

function errorResponse(res, identity, status, code, message, extraHeaders = {}) {
  for (const [name, value] of Object.entries(extraHeaders)) {
    res.setHeader(name, value);
  }
  const body = { code, message };
  if (identity === "browser") {
    body.error = message;
  }
  return res.status(status).json(body);
}

function regexForPath(template) {
  const escaped = template.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`^${escaped.replace(/\\\{([a-z_]+)\\\}/g, "([^/]+)")}$`);
}

const compiledRoutes = routes.map((route) => ({
  row: route,
  identity: route.identity,
  method: route.method,
  regex: regexForPath(route.inner_path),
  parameterNames: [...route.inner_path.matchAll(/\{([a-z_]+)\}/g)].map((match) => match[1]),
}));

function routeMatches(route, rawPath, identity) {
  const match = route.regex.exec(rawPath);
  if (!match) {
    return false;
  }
  for (let index = 0; index < route.parameterNames.length; index += 1) {
    const name = route.parameterNames[index];
    let segment = match[index + 1];
    if (identity === "browser") {
      try {
        segment = decodeURIComponent(segment);
      } catch {
        return false;
      }
    }
    const definition = pathParams[name];
    const pattern = identity === "browser" ? definition.browser_pattern : definition.gateway_pattern;
    if (!pattern || !new RegExp(pattern, "u" + (definition.flags || "")).test(segment)) {
      return false;
    }
  }
  return true;
}

function isNeverAllowed(pathname) {
  return neverAllowed.some(({ inner_path: pattern }) => {
    if (pattern.endsWith("/*")) {
      return pathname.startsWith(pattern.slice(0, -1));
    }
    return pathname === pattern;
  });
}

function createAuthMiddleware(env = process.env) {
  const catalog = tokenCatalog(env);
  const actorPattern = new RegExp(actorHeaders.actor_pattern, "u");
  const fingerprintPattern = new RegExp(actorHeaders.fingerprint_pattern, "u");

  return function watcherAuth(req, res, next) {
    const authorization = rawValues(req, "authorization");
    const proxy = rawValues(req, "x-watcher-proxy-auth");
    const actors = rawValues(req, "x-watcher-actor");
    const fingerprints = rawValues(req, "x-watcher-token-fingerprint");
    if (authorization.length > 1 || proxy.length > 1) {
      return errorResponse(res, null, 401, "unauthenticated", "authentication required");
    }

    let identity = null;
    if (authorization.length === 1 && authorization[0].startsWith("Bearer ")) {
      if (proxy.length !== 0) {
        return errorResponse(res, null, 401, "unauthenticated", "authentication required");
      }
      const candidate = stripToken(authorization[0].slice("Bearer ".length));
      if (!candidate) {
        return errorResponse(res, null, 401, "unauthenticated", "authentication required");
      }
      const gatewayMatch = matchesToken(candidate, catalog.gateway);
      const snapshotMatch = matchesToken(candidate, catalog.snapshot);
      if (gatewayMatch) {
        identity = "gateway";
      } else if (snapshotMatch) {
        identity = "snapshot";
      }
    } else if (proxy.length === 1 && (authorization.length === 0 || authorization[0].split(" ", 1)[0].toLowerCase() === "basic")) {
      const candidate = stripToken(proxy[0]);
      if (matchesToken(candidate, catalog.browser)) {
        identity = "browser";
      }
    }
    if (!identity) {
      return errorResponse(res, null, 401, "unauthenticated", "authentication required");
    }

    const pathname = req.originalUrl.split("?")[0];
    if (identity !== "browser" && (pathname.includes("%") || pathname.includes("//") || (pathname !== "/" && pathname.endsWith("/")) || pathname.split("/").some((part) => part === "." || part === ".."))) {
      return errorResponse(res, identity, 404, "route_not_found", "route not found");
    }
    if (identity === "gateway" && isNeverAllowed(pathname)) {
      return errorResponse(res, identity, 403, "identity_forbidden", "identity forbidden");
    }
    const matches = compiledRoutes.filter((route) => routeMatches(route, pathname, route.identity));
    const own = matches.filter((route) => route.identity === identity);
    if (own.length === 0) {
      if (matches.length > 0) {
        return errorResponse(res, identity, 403, "identity_forbidden", "identity forbidden");
      }
      return errorResponse(res, identity, 404, "route_not_found", "route not found");
    }
    const selected = own.find((route) => route.method === req.method);
    if (!selected) {
      const allow = [...new Set(own.map((route) => route.method))].sort().join(", ");
      return errorResponse(res, identity, 405, "method_not_allowed", "method not allowed", { Allow: allow });
    }

    let auth;
    if (identity === "gateway") {
      if (actors.length !== 1 || fingerprints.length !== 1 || !actorPattern.test(actors[0]) || !fingerprintPattern.test(fingerprints[0])) {
        return errorResponse(res, identity, 400, "invalid_actor_headers", "invalid actor headers");
      }
      const role = actors[0].slice("app:".length);
      if (selected.row.write && role !== "risk_admin") {
        return errorResponse(res, identity, 403, "identity_forbidden", "identity forbidden");
      }
      auth = { identity, role, actor: actors[0], tokenFingerprint: fingerprints[0] };
    } else {
      if (actors.length !== 0 || fingerprints.length !== 0) {
        return errorResponse(res, identity, 400, "invalid_actor_headers", "invalid actor headers");
      }
      auth = {
        identity,
        role: null,
        actor: identity === "browser" ? "browser" : "snapshot",
        tokenFingerprint: null,
      };
    }
    Object.defineProperty(req, "watcherAuth", { value: Object.freeze(auth), writable: false, configurable: false, enumerable: true });
    Object.defineProperty(req, "watcherRoute", { value: selected.row, writable: false, configurable: false, enumerable: true });
    return next();
  };
}

module.exports = { createAuthMiddleware, tokenCatalog, matchesToken, isNeverAllowed, stripToken };
