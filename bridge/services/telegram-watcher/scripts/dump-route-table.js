#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

// Route registration needs an isolated SQLite schema. Never inspect a live DB.
const root = fs.mkdtempSync(path.join(os.tmpdir(), "watcher-route-table-"));
process.env.TRADER_TRADING_DB_PATH = path.join(root, "routes.db");
process.env.WATCHER_MEDIA_DIR = path.join(root, "media");
for (const [name, value] of Object.entries({ WATCHER_GATEWAY_TOKEN: "g", WATCHER_SNAPSHOT_TOKEN: "s", WATCHER_BROWSER_PROXY_TOKEN: "b" })) {
  process.env[name] = value.repeat(40);
  delete process.env[`${name}_PREVIOUS`];
}

try {
  const { app } = require("../server");
  const { registerTradingApi } = require("../lib/trading-api");
  registerTradingApi(app, {});
  const authLayer = app.router.stack.find((layer) => Array.isArray(layer.handle?.routeTable));
  if (!authLayer) {
    throw new Error("watcher authentication route table is not mounted");
  }
  const handlers = [];
  for (const layer of app.router.stack) {
    if (!layer.route) {
      continue;
    }
    for (const method of Object.keys(layer.route.methods)) {
      handlers.push({ method: method.toUpperCase(), path: layer.route.path });
    }
  }
  // Express static and mounted media are middleware, so they have no route entry.
  const staticPaths = ["/", "/index.html"];
  const mediaPaths = ["/media/{filename}"];
  function sameShape(actual, template) {
    const left = actual.split("/");
    const right = template.split("/");
    return left.length === right.length && left.every((segment, index) => segment.startsWith(":") || right[index].startsWith("{") || segment === right[index]);
  }
  const identityRoutes = authLayer.handle.routeTable.map(({ identity, method, inner_path: innerPath }) => ({ identity, method, inner_path: innerPath }));
  const missing = identityRoutes.filter((row) => {
    if (mediaPaths.includes(row.inner_path) || staticPaths.includes(row.inner_path)) {
      return false;
    }
    return !handlers.some((handler) => handler.method === row.method && sameShape(handler.path, row.inner_path));
  });
  const unexpected = handlers.filter((handler) => (handler.path.startsWith("/api/") || handler.path.startsWith("/media/")) && !identityRoutes.some((row) => row.method === handler.method && sameShape(handler.path, row.inner_path)));
  process.stdout.write(JSON.stringify({ identity_routes: identityRoutes, handlers, static_paths: staticPaths, media_paths: mediaPaths, missing, unexpected }) + "\n");
  if (missing.length || unexpected.length) {
    process.exitCode = 1;
  }
} finally {
  fs.rmSync(root, { recursive: true, force: true });
}
