"""Hermes multimodal client.

Hermes is the *only* semantic processor in the system. This module exposes a
pluggable client contract plus a real OpenAI-compatible multimodal implementation.
There is intentionally no regex/OCR fallback: if Hermes is unavailable the worker
fails closed (no decision is produced).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol


class HermesError(RuntimeError):
    """Base class for Hermes client failures."""


class HermesUnavailableError(HermesError):
    """Hermes endpoint/credentials are not configured or unreachable."""


class HermesTimeoutError(HermesError):
    """Hermes did not respond within the deadline."""


class HermesResponseError(HermesError):
    """Hermes returned a non-JSON / malformed response."""


@dataclass(frozen=True)
class HermesImage:
    sha256: str
    mime: str
    # base64-encoded bytes; the worker is responsible for fetching real bytes.
    data_base64: str


@dataclass(frozen=True)
class HermesRequest:
    raw_message_id: str
    text: str
    images: list[HermesImage] = field(default_factory=list)
    referenced_messages: list[dict[str, Any]] = field(default_factory=list)
    recent_context: list[dict[str, Any]] = field(default_factory=list)
    system_snapshot: dict[str, Any] | None = None


class HermesClient(Protocol):
    def analyze(self, request: HermesRequest, *, timeout: float) -> dict[str, Any]:
        """Return a candidate HermesDecisionV1 dict (unvalidated)."""
        ...


class RealHermesClient:
    """OpenAI-compatible multimodal chat-completions client.

    Configured purely from the environment; raises HermesUnavailableError when the
    endpoint or key is missing so the worker fails closed instead of guessing.
    """

    def __init__(
        self,
        *,
        api_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self._api_url = (api_url if api_url is not None else os.environ.get("HERMES_API_URL", "")).strip()
        self._api_key = (api_key if api_key is not None else os.environ.get("HERMES_API_KEY", "")).strip()
        self._model = (model if model is not None else os.environ.get("HERMES_MODEL", "")).strip()

    @property
    def configured(self) -> bool:
        return bool(self._api_url and self._api_key and self._model)

    def analyze(self, request: HermesRequest, *, timeout: float) -> dict[str, Any]:
        if not self.configured:
            raise HermesUnavailableError(
                "HERMES_API_URL / HERMES_API_KEY / HERMES_MODEL are not all set"
            )

        # Imported lazily so this module has no hard dependency on prompt assembly.
        from prompt import MODEL_TEMPERATURE, PROMPT_VERSION, build_messages

        body = {
            "model": self._model,
            "temperature": MODEL_TEMPERATURE,
            "response_format": {"type": "json_object"},
            "messages": build_messages(request),
            "metadata": {"prompt_version": PROMPT_VERSION},
        }
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self._chat_completions_url(),
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                raise HermesTimeoutError(str(reason)) from exc
            raise HermesUnavailableError(str(reason)) from exc
        except TimeoutError as exc:  # pragma: no cover - platform dependent
            raise HermesTimeoutError(str(exc)) from exc
        except json.JSONDecodeError as exc:
            raise HermesResponseError(f"non-JSON transport response: {exc}") from exc

        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise HermesResponseError(f"unexpected response shape: {exc}") from exc

        try:
            return json.loads(content)
        except (TypeError, json.JSONDecodeError) as exc:
            raise HermesResponseError(f"model content is not valid JSON: {exc}") from exc

    def _chat_completions_url(self) -> str:
        base = self._api_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"
