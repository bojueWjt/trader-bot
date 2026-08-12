from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass(frozen=True)
class ReleaseVersionResponse:
    status_code: int
    body: dict[str, object]


def build_release_version_response(
    *,
    started_at: datetime | None = None,
) -> ReleaseVersionResponse:
    started = started_at
    if started is None:
        started = datetime.now(timezone.utc)
    fields = {
        "git_sha": os.environ.get("TRADER_RELEASE_COMMIT", "").strip(),
        "image_digest": os.environ.get(
            "TRADER_RELEASE_IMAGE_DIGEST",
            "",
        ).strip(),
        "build_id": os.environ.get("TRADER_RELEASE_ID", "").strip(),
        "dependency_lock_sha256": os.environ.get(
            "TRADER_RELEASE_DEPENDENCY_LOCK_SHA256",
            "",
        ).strip(),
        "config_sha256": os.environ.get(
            "TRADER_RELEASE_CONFIG_SHA256",
            "",
        ).strip(),
        "schema_epoch": os.environ.get(
            "TRADER_RELEASE_SCHEMA_EPOCH",
            "",
        ).strip(),
        "started_at": started.astimezone(timezone.utc).isoformat(),
    }
    missing = [
        key
        for key, value in fields.items()
        if key != "started_at" and not value
    ]
    body: dict[str, object] = dict(fields)
    body["complete"] = not missing
    body["missing"] = missing
    status_code = 200
    if missing:
        status_code = 503
    return ReleaseVersionResponse(
        status_code=status_code,
        body=body,
    )


def build_health_server(runtime: Any, host: str, port: int) -> ThreadingHTTPServer:
    version_response = build_release_version_response()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            if self.path == "/live":
                response = runtime.health.liveness()
            elif self.path == "/ready":
                response = runtime.health.readiness()
            elif self.path == "/version":
                response = version_response
            else:
                self.send_error(404)
                return
            body = json.dumps(response.body, sort_keys=True).encode("utf-8")
            self.send_response(response.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return ThreadingHTTPServer((host, port), Handler)
