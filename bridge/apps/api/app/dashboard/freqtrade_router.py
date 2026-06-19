from __future__ import annotations

from urllib.parse import quote

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from app.dependencies import require_bearer_user
from app.settings import get_settings


router = APIRouter()
ALLOWED_ROOTS = {"status", "trades", "positions"}


@router.get("/{proxy_path:path}")
def proxy_get(
    proxy_path: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    require_bearer_user(authorization)
    if not _is_allowed_path(proxy_path):
        raise HTTPException(status_code=404, detail="freqtrade path not found")

    upstream_url = _upstream_url(proxy_path)
    try:
        with build_freqtrade_client() as client:
            upstream_response = client.get(
                upstream_url,
                params=list(request.query_params.multi_items()),
            )
    except httpx.TimeoutException as exc:
        return JSONResponse(
            status_code=503,
            content={"error": "timeout", "message": "freqtrade upstream timed out"},
        )
    except httpx.ConnectError as exc:
        return JSONResponse(
            status_code=503,
            content={"error": "connection_error", "message": "freqtrade upstream connection failed"},
        )
    except httpx.RequestError as exc:
        return JSONResponse(
            status_code=503,
            content={"error": "request_error", "message": f"freqtrade upstream request failed: {exc}"},
        )

    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        media_type=upstream_response.headers.get("content-type"),
    )


@router.api_route("/{proxy_path:path}", methods=["POST", "PUT", "DELETE", "PATCH"])
def proxy_write_not_allowed(proxy_path: str) -> None:
    raise HTTPException(status_code=405, detail="freqtrade proxy is read-only")


def build_freqtrade_client() -> httpx.Client:
    settings = get_settings()
    auth = None
    if settings.freqtrade_api_user and settings.freqtrade_api_password:
        auth = (settings.freqtrade_api_user, settings.freqtrade_api_password)
    return httpx.Client(timeout=2.0, auth=auth)


def _is_allowed_path(proxy_path: str) -> bool:
    parts = [part for part in proxy_path.split("/") if part]
    return bool(parts) and parts[0] in ALLOWED_ROOTS


def _upstream_url(proxy_path: str) -> str:
    base_url = get_settings().freqtrade_base_url.rstrip("/")
    encoded_path = "/".join(quote(part, safe="") for part in proxy_path.split("/"))
    return f"{base_url}/{encoded_path}"
