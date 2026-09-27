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
  const known = { auth: new WeakSet(), validation: new WeakSet(), static: new WeakSet(), media: new WeakSet() };
  function captureFactory(module, key, handlers) {
    const original = module[key];
    module[key] = function (...args) {
      const handler = original.apply(this, args);
      handlers.add(handler);
      return handler;
    };
  }
  captureFactory(require("../lib/auth"), "createAuthMiddleware", known.auth);
  captureFactory(require("../lib/request-validation"), "createRequestValidation", known.validation);
  captureFactory(require("../lib/media"), "createMediaHandler", known.media);
  captureFactory(express, "static", known.static);
  const originalUse = express.application.use;
  express.application.use = function (...args) {
    const implicitRoot = typeof args[0] === "function";
    const mount = implicitRoot ? "/" : args[0];
    const handlers = implicitRoot ? args : args.slice(1);
    for (const handler of handlers) {
      mounts.push({ path: mount, handler });
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
  function collectRoutes(stack) {
    for (const layer of stack) {
      if (layer.route) {
        for (const method of Object.keys(layer.route.methods)) {
          handlers.push({ method: method.toUpperCase(), path: layer.route.path });
        }
      }
      if (Array.isArray(layer.handle?.stack)) {
        collectRoutes(layer.handle.stack);
      }
    }
  }
  collectRoutes(app.router.stack);
  // Express does not retain the mount string on middleware layers. Capture it at registration.
  const staticMounts = mounts.filter((mount) => mount.path === "/" && known.static.has(mount.handler)).map((mount) => mount.path);
  const mediaMounts = mounts.filter((mount) => mount.path === "/media" && known.media.has(mount.handler)).map((mount) => mount.path);
  const staticPaths = staticMounts.flatMap((mount) => [mount, path.posix.join(mount, "index.html")]);
  const mediaPaths = mediaMounts.map((mount) => path.posix.join(mount, "{filename}"));
  function sameShape(actual, template) {
    const left = actual.toLowerCase().split("/");
    const right = template.toLowerCase().split("/");
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
  const unexpected = handlers.filter((handler) => {
    const routePath = typeof handler.path === "string" ? handler.path.toLowerCase() : "";
    return (routePath === "/api" || routePath.startsWith("/api/") || routePath === "/media" || routePath.startsWith("/media/")) && !identityRoutes.some((row) => row.method === handler.method && sameShape(handler.path, row.inner_path));
  });
  for (const mount of mounts) {
    const allowed = typeof mount.path === "string" && (
      (mount.path === "/" && (known.auth.has(mount.handler) || known.validation.has(mount.handler) || known.static.has(mount.handler))) ||
      (mount.path === "/media" && known.media.has(mount.handler))
    );
    if (!allowed) {
      unexpected.push({ method: "USE", path: typeof mount.path === "string" ? mount.path : String(mount.path) });
    }
  }
  process.stdout.write(JSON.stringify({ identity_routes: identityRoutes, handlers, static_paths: staticPaths, media_paths: mediaPaths, missing, unexpected }) + "\n");
  if (missing.length || unexpected.length) {
    process.exitCode = 1;
  }
} finally {
  fs.rmSync(root, { recursive: true, force: true });
}
