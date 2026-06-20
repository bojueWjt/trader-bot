from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class IngressDeliveryError(RuntimeError):
    pass


class HTTPIngressClient:
    def __init__(self, endpoint: str, timeout_seconds: float = 10, token: str | None = None):
        self.endpoint = endpoint.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.token = token if token is not None else (os.environ.get("INGRESS_API_TOKEN", "").strip() or None)

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        headers = {"content-type": "application/json"}
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        request = Request(
            f"{self.endpoint}/telegram/raw",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise IngressDeliveryError(f"ingress returned HTTP {exc.code}: {error_body}") from exc
        except URLError as exc:
            raise IngressDeliveryError(f"ingress request failed: {exc.reason}") from exc

        data = json.loads(response_body)
        if not isinstance(data, dict):
            raise IngressDeliveryError("ingress response must be an object")
        return data
