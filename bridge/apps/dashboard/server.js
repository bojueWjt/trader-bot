import { createReadStream, existsSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = fileURLToPath(new URL(".", import.meta.url));
const distDir = resolve(__dirname, "dist");
const port = Number.parseInt(process.env.PORT || "3000", 10);
const freqtradeApiUrl = String(process.env.FREQTRADE_API_URL || "http://127.0.0.1:8080").replace(/\/+$/, "");
const apiUsername = String(process.env.FREQTRADE_API_USERNAME || "");
const apiPassword = String(process.env.FREQTRADE_API_PASSWORD || "");
const dashboardAuthDisabled = /^(1|true|on|yes)$/i.test(String(process.env.HERMES_DASHBOARD_AUTH_DISABLED || ""));

const mimeTypes = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".map": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".txt": "text/plain; charset=utf-8",
};

function sendJson(response, statusCode, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(statusCode, {
    "Content-Length": Buffer.byteLength(body),
    "Content-Type": "application/json; charset=utf-8",
  });
  response.end(body);
}

function authHeader() {
  if (!apiUsername || !apiPassword) {
    return false;
  }

  const token = Buffer.from(`${apiUsername}:${apiPassword}`, "utf8").toString("base64");
  return `Basic ${token}`;
}

function unauthorized(response) {
  response.writeHead(401, {
    "Content-Type": "text/plain; charset=utf-8",
    "WWW-Authenticate": 'Basic realm="Freqtrade"',
  });
  response.end("Authentication required\n");
}

function requestAuthorized(request) {
  if (dashboardAuthDisabled) {
    return true;
  }

  if (!apiUsername || !apiPassword) {
    return false;
  }

  const expected = authHeader();
  const received = request.headers.authorization || "";
  return received === expected;
}

function requireDashboardAuth(request, response) {
  if (requestAuthorized(request)) {
    return true;
  }

  unauthorized(response);
  return false;
}

function requestBody(request) {
  return new Promise((resolveBody, rejectBody) => {
    const chunks = [];
    request.on("data", (chunk) => {
      chunks.push(chunk);
    });
    request.on("end", () => {
      resolveBody(Buffer.concat(chunks));
    });
    request.on("error", rejectBody);
  });
}

function proxyHeaders(request) {
  const headers = {};
  for (const [key, value] of Object.entries(request.headers)) {
    const normalizedKey = key.toLowerCase();
    if (/^(host|connection|content-length|authorization)$/.test(normalizedKey)) {
      continue;
    }

    if (value) {
      headers[key] = value;
    }
  }

  const authorization = authHeader();
  if (authorization) {
    headers.Authorization = authorization;
  }

  return headers;
}

async function proxyApi(request, response, url) {
  if (!authHeader()) {
    sendJson(response, 503, {
      error: "dashboard_proxy_credentials_missing",
    });
    return;
  }

  const body = await requestBody(request);
  const method = request.method || "GET";
  const targetUrl = `${freqtradeApiUrl}${url.pathname}${url.search}`;
  const init = {
    headers: proxyHeaders(request),
    method,
    redirect: "manual",
  };

  if (!/^(GET|HEAD)$/i.test(method)) {
    init.body = body;
  }

  const upstream = await fetch(targetUrl, init);
  const responseHeaders = {};
  upstream.headers.forEach((value, key) => {
    if (/^(connection|content-encoding|content-length|transfer-encoding)$/i.test(key)) {
      return;
    }

    responseHeaders[key] = value;
  });

  response.writeHead(upstream.status, responseHeaders);
  if (!upstream.body) {
    response.end();
    return;
  }

  const reader = upstream.body.getReader();
  while (true) {
    const next = await reader.read();
    if (next.done) {
      break;
    }

    response.write(Buffer.from(next.value));
  }
  response.end();
}

function safeStaticPath(pathname) {
  const decodedPath = decodeURIComponent(pathname);
  const normalizedPath = normalize(decodedPath).replace(/^(\.\.[/\\])+/, "");
  const relativePath = normalizedPath.replace(/^[/\\]+/, "");
  const filePath = resolve(distDir, relativePath);

  if (!filePath.startsWith(distDir)) {
    return false;
  }

  return filePath;
}

function serveFile(response, filePath) {
  if (!existsSync(filePath)) {
    return false;
  }

  const stat = statSync(filePath);
  if (!stat.isFile()) {
    return false;
  }

  const extension = extname(filePath);
  const contentType = mimeTypes[extension] || "application/octet-stream";
  response.writeHead(200, {
    "Cache-Control": extension === ".html" ? "no-store" : "public, max-age=31536000, immutable",
    "Content-Length": stat.size,
    "Content-Type": contentType,
  });
  createReadStream(filePath).pipe(response);
  return true;
}

async function handleRequest(request, response) {
  const rawUrl = request.url || "/";
  const url = new URL(rawUrl, "http://127.0.0.1");

  if (url.pathname === "/healthz") {
    sendJson(response, 200, {
      status: "ok",
    });
    return;
  }

  if (!requireDashboardAuth(request, response)) {
    return;
  }

  if (/^\/api(\/|$)/.test(url.pathname)) {
    try {
      await proxyApi(request, response, url);
    } catch (error) {
      sendJson(response, 502, {
        error: "dashboard_proxy_upstream_failed",
      });
    }
    return;
  }

  const staticPath = safeStaticPath(url.pathname);
  if (staticPath && serveFile(response, staticPath)) {
    return;
  }

  const indexPath = join(distDir, "index.html");
  if (serveFile(response, indexPath)) {
    return;
  }

  sendJson(response, 503, {
    error: "dashboard_build_missing",
  });
}

createServer((request, response) => {
  handleRequest(request, response);
}).listen(port, "0.0.0.0", () => {
  console.log(`Hermes dashboard listening on ${port}`);
});
