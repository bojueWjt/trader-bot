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
  const express = require("express");
  const mounts = [];
  const originalUse = express.application.use;
  express.application.use = function (...args) {
    const mount = typeof args[0] === "string" ? args[0] : "/";
    const handlers = typeof args[0] === "string" ? args.slice(1) : args;
    for (const handler of handlers) {
      mounts.push({ path: mount, name: handler.name });
    }
    return originalUse.apply(this, args);
  };
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
  // Express does not retain the mount string on middleware layers. Capture it at registration.
  const staticMounts = mounts.filter((mount) => mount.name === "serveStatic").map((mount) => mount.path);
  const mediaMounts = mounts.filter((mount) => mount.name === "mediaHandler").map((mount) => mount.path);
  const staticPaths = staticMounts.flatMap((mount) => [mount, path.posix.join(mount, "index.html")]);
  const mediaPaths = mediaMounts.map((mount) => path.posix.join(mount, "{filename}"));
  function sameShape(actual, template) {
    const left = actual.split("/");
    const right = template.split("/");
    return left.length === right.length && left.every((segment, index) => {
      const parameter = /^:([A-Za-z][A-Za-z0-9_]*)$/u.exec(segment);
      return parameter ? /^\{[a-z][a-z0-9_]*\}$/u.test(right[index]) : segment === right[index];
    });
  }
  const identityRoutes = authLayer.handle.routeTable.map(({ identity, method, inner_path: innerPath }) => ({ identity, method, inner_path: innerPath }));
  const missing = identityRoutes.filter((row) => {
    if (mediaPaths.includes(row.inner_path) || staticPaths.includes(row.inner_path)) {
      return false;
    }
    return !handlers.some((handler) => handler.method === row.method && sameShape(handler.path, row.inner_path));
  });
  const unexpected = handlers.filter((handler) => (handler.path.startsWith("/api/") || handler.path.startsWith("/media/")) && !identityRoutes.some((row) => row.method === handler.method && sameShape(handler.path, row.inner_path)));
  for (const mount of mounts) {
    if ((mount.path.startsWith("/api/") || mount.path.startsWith("/media/")) && !mediaMounts.includes(mount.path)) {
      unexpected.push({ method: "USE", path: mount.path });
    }
  }
  process.stdout.write(JSON.stringify({ identity_routes: identityRoutes, handlers, static_paths: staticPaths, media_paths: mediaPaths, missing, unexpected }) + "\n");
  if (missing.length || unexpected.length) {
    process.exitCode = 1;
  }
} finally {
  fs.rmSync(root, { recursive: true, force: true });
}
