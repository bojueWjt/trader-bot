const { requireWatcherContext } = require("./request-validation");
const fs = require("node:fs");
const path = require("node:path");

const MAX_FILE_BYTES = 20 * 1024 * 1024;
const CONTENT_TYPES = {
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  png: "image/png",
  gif: "image/gif",
  webp: "image/webp",
};

function parseRange(value, size) {
  const match = /^bytes=(\d*)-(\d*)$/.exec(value);
  if (!match || (!match[1] && !match[2])) {
    return null;
  }
  const startText = match[1];
  const endText = match[2];
  if (!startText) {
    const suffix = Number(endText);
    if (!Number.isSafeInteger(suffix) || suffix <= 0 || size === 0) {
      return null;
    }
    return { start: Math.max(size - suffix, 0), end: size - 1 };
  }
  const start = Number(startText);
  const end = endText ? Number(endText) : size - 1;
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start >= size || start > end) {
    return null;
  }
  return { start, end: Math.min(end, size - 1) };
}

function fail(res, status, code, message, identity) {
  res.setHeader("Cache-Control", "private, no-store");
  const body = { code, message };
  if (identity === "browser") {
    body.error = message;
  }
  return res.status(status).json(body);
}

function createMediaHandler(mediaDir) {
  return async function mediaHandler(req, res) {
    let identity;
    try {
      identity = requireWatcherContext(req).auth.identity;
    } catch {
      return fail(res, 500, "internal_error", "watcher request context unavailable", null);
    }
    const filename = req.path.slice(1);
    if (!filename || filename.includes("/") || filename.includes("\\") || filename === "." || filename === "..") {
      return fail(res, 404, "not_found", "media not found", identity);
    }
    let fileHandle;
    try {
      const root = await fs.promises.realpath(mediaDir);
      const actual = await fs.promises.realpath(path.join(root, filename));
      const relative = path.relative(root, actual);
      if (!relative || relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
        return fail(res, 404, "not_found", "media not found", identity);
      }
      fileHandle = await fs.promises.open(actual, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
      const stat = await fileHandle.stat();
      if (!stat.isFile()) {
        return fail(res, 404, "not_found", "media not found", identity);
      }
      if (stat.size > MAX_FILE_BYTES) {
        return fail(res, 503, "media_too_large", "media too large", identity);
      }
      const extension = path.extname(filename).slice(1).toLowerCase();
      const contentType = CONTENT_TYPES[extension] || "application/octet-stream";
      const etag = `W/"${stat.size.toString(16)}-${Math.trunc(stat.mtimeMs).toString(16)}"`;
      const lastModified = stat.mtime.toUTCString();
      res.setHeader("Accept-Ranges", "bytes");
      res.setHeader("Content-Type", contentType);
      res.setHeader("ETag", etag);
      res.setHeader("Last-Modified", lastModified);
      res.setHeader("Cache-Control", "private, no-store");

      if (req.method === "HEAD") {
        res.setHeader("Content-Length", String(stat.size));
        res.status(200).end();
        return;
      }

      const rawRanges = [];
      for (let index = 0; index < req.rawHeaders.length; index += 2) {
        if (req.rawHeaders[index].toLowerCase() === "range") {
          rawRanges.push(req.rawHeaders[index + 1]);
        }
      }
      let range = null;
      if (rawRanges.length > 0) {
        if (rawRanges.length !== 1) {
          res.setHeader("Content-Range", `bytes */${stat.size}`);
          return fail(res, 416, "range_not_satisfiable", "range not satisfiable", identity);
        }
        range = parseRange(rawRanges[0], stat.size);
        if (!range) {
          res.setHeader("Content-Range", `bytes */${stat.size}`);
          return fail(res, 416, "range_not_satisfiable", "range not satisfiable", identity);
        }
        const ifRange = req.get("If-Range");
        if (ifRange && ifRange !== etag && ifRange !== lastModified) {
          range = null;
        }
      }
      const start = range ? range.start : 0;
      const end = range ? range.end : stat.size - 1;
      if (range) {
        res.setHeader("Content-Range", `bytes ${start}-${end}/${stat.size}`);
      }
      res.setHeader("Content-Length", String(range ? end - start + 1 : stat.size));
      res.status(range ? 206 : 200);
      if (stat.size === 0) {
        res.end();
        return;
      }
      const handle = fileHandle;
      const stream = handle.createReadStream({ start, end, highWaterMark: 64 * 1024, autoClose: false });
      let bytesSent = 0;
      let truncated = false;
      stream.on("data", (chunk) => { bytesSent += chunk.length; });
      function logTruncated(reason) {
        if (!truncated) {
          truncated = true;
          console.error(`[media] truncated filename=${filename} bytes_sent=${bytesSent} reason=${reason}`);
        }
      }
      const timer = setTimeout(() => stream.destroy(new Error("media timeout")), 60000);
      stream.on("error", () => {
        if (res.headersSent) {
          logTruncated("stream_error");
          res.destroy();
        } else {
          fail(res, 503, "watcher_unavailable", "media unavailable", identity);
        }
      });
      res.on("close", () => {
        if (!res.writableFinished) {
          logTruncated("client_closed");
        }
        clearTimeout(timer);
        stream.destroy();
        handle.close().catch(() => {});
      });
      stream.on("end", () => {
        clearTimeout(timer);
        handle.close().catch(() => {});
      });
      stream.pipe(res);
      fileHandle = null;
    } catch {
      if (!res.headersSent) {
        return fail(res, 404, "not_found", "media not found", identity);
      }
      res.destroy();
    } finally {
      if (fileHandle) {
        await fileHandle.close();
      }
    }
  };
}

module.exports = { createMediaHandler, parseRange };
