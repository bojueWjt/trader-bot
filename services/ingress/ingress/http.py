from __future__ import annotations

import argparse
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .service import IngressValidationError, ingest_raw_telegram_update


class IngressHTTPHandler(BaseHTTPRequestHandler):
    database_url: str | None = None
    expected_token: str | None = None

    def do_POST(self) -> None:
        if not self._authorized():
            self._write_json(401, {"error": "unauthorized"})
            return
        if self.path != "/telegram/raw":
            self._write_json(404, {"error": "not_found"})
            return

        try:
            payload = self._read_json()
            result = ingest_raw_telegram_update(payload, self.database_url)
        except IngressValidationError as exc:
            self._write_json(400, {"error": "invalid_payload", "message": str(exc)})
            return
        except Exception as exc:
            self._write_json(500, {"error": "ingress_failed", "message": str(exc)})
            return

        self._write_json(201 if result["inserted"] else 200, result)

    def _authorized(self) -> bool:
        # Fail closed: the raw-message write path requires a shared secret.
        # No token configured -> deny everything (no anonymous write).
        expected = self.expected_token
        if not expected:
            return False
        header = self.headers.get("authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        return hmac.compare_digest(header[len(prefix):].strip(), expected)

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        data = json.loads(body.decode("utf-8"))
        if not isinstance(data, dict):
            raise IngressValidationError("request body must be an object")
        return data

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _require_ingress_token() -> str:
    # Secret fail-closed: refuse to start the writer without a configured token.
    token = os.environ.get("INGRESS_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("INGRESS_API_TOKEN is required (fail-closed; no default)")
    return token


def run(host: str, port: int, database_url: str | None = None) -> None:
    IngressHTTPHandler.database_url = database_url
    IngressHTTPHandler.expected_token = _require_ingress_token()
    server = ThreadingHTTPServer((host, port), IngressHTTPHandler)
    server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description="Raw Telegram ingress service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8087)
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args()
    run(args.host, args.port, args.database_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
