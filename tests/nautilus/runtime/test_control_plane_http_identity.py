from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

import execution_domain.http_client as http_client_module  # noqa: E402
from execution_domain.contracts import ReconciliationState  # noqa: E402
from execution_domain.control_plane import Heartbeat, TradingState  # noqa: E402
from execution_domain.http_client import (  # noqa: E402
    ControlPlaneFenceConflictError,
    ControlPlaneHttpError,
    ControlPlaneIdentityError,
    HttpControlPlaneClient,
)


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def read(self) -> bytes:
        return self._payload


def test_intent_pull_carries_bound_node_and_account_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = []

    def urlopen(request, timeout):
        del timeout
        requests.append(request)
        return _Response({"items": [], "next_cursor": None})

    monkeypatch.setattr(http_client_module, "urlopen", urlopen)
    client = _client()

    client.fetch_intents(
        account_id="account-a",
        after_cursor=None,
        limit=50,
    )

    request = requests[0]
    query = parse_qs(urlparse(request.full_url).query)
    assert query["account_id"] == ["account-a"]
    assert request.get_header("X-node-id") == "node-a"
    assert request.get_header("X-account-id") == "account-a"


def test_commands_and_heartbeat_carry_account_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = []

    def urlopen(request, timeout):
        del timeout
        requests.append(request)
        path = urlparse(request.full_url).path
        if path.endswith("/commands"):
            return _Response({"commands": []})
        return _Response({})

    monkeypatch.setattr(http_client_module, "urlopen", urlopen)
    client = _client()

    client.poll_commands("node-a", None)
    client.heartbeat(
        "node-a",
        Heartbeat(
            account_id="account-a",
            ts=datetime(2026, 7, 29, tzinfo=timezone.utc),
            trading_state=TradingState.HALTED,
            readiness=False,
            projection_lag_ms=0,
            reconciliation_state=ReconciliationState.DEGRADED,
        ),
    )

    command_query = parse_qs(urlparse(requests[0].full_url).query)
    heartbeat_body = json.loads(requests[1].data)
    assert command_query["account_id"] == ["account-a"]
    assert command_query["limit"] == ["100"]
    assert heartbeat_body["account_id"] == "account-a"
    assert requests[1].get_header("X-account-id") == "account-a"


def test_command_poll_clamps_limit_and_carries_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = []

    def urlopen(request, timeout):
        del timeout
        requests.append(request)
        return _Response({"commands": []})

    monkeypatch.setattr(http_client_module, "urlopen", urlopen)

    _client().poll_commands(
        "node-a",
        after="command-100",
        limit=50_000,
    )

    query = parse_qs(urlparse(requests[0].full_url).query)
    assert query["account_id"] == ["account-a"]
    assert query["after"] == ["command-100"]
    assert query["limit"] == ["500"]


def test_http_409_raises_typed_fence_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def urlopen(request, timeout):
        del timeout
        raise HTTPError(
            request.full_url,
            409,
            "Conflict",
            hdrs={},
            fp=BytesIO(
                b'{"detail":{"code":"lease_owner_conflict",'
                b'"message":"lease owner conflict"}}'
            ),
        )

    monkeypatch.setattr(http_client_module, "urlopen", urlopen)

    with pytest.raises(ControlPlaneFenceConflictError) as captured:
        _client().poll_commands("node-a", None)

    assert captured.value.status_code == 409


def test_http_503_preserves_status_as_recoverable_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def urlopen(request, timeout):
        del timeout
        raise HTTPError(
            request.full_url,
            503,
            "Service Unavailable",
            hdrs={},
            fp=BytesIO(b'{"detail":"store unavailable"}'),
        )

    monkeypatch.setattr(http_client_module, "urlopen", urlopen)

    with pytest.raises(ControlPlaneHttpError) as captured:
        _client().poll_commands("node-a", None)

    assert type(captured.value) is ControlPlaneHttpError
    assert captured.value.status_code == 503


def test_non_fencing_http_409_remains_recoverable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def urlopen(request, timeout):
        del timeout
        raise HTTPError(
            request.full_url,
            409,
            "Conflict",
            hdrs={},
            fp=BytesIO(
                b'{"detail":"release version conflict"}'
            ),
        )

    monkeypatch.setattr(http_client_module, "urlopen", urlopen)

    with pytest.raises(ControlPlaneHttpError) as captured:
        _client().poll_commands("node-a", None)

    assert type(captured.value) is ControlPlaneHttpError
    assert captured.value.status_code == 409


def test_client_rejects_cross_account_request_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_urlopen(request, timeout):
        del request, timeout
        raise AssertionError("network request must not be attempted")

    monkeypatch.setattr(http_client_module, "urlopen", unexpected_urlopen)
    client = _client()

    with pytest.raises(ControlPlaneIdentityError, match="account_id mismatch"):
        client.fetch_intents(
            account_id="account-b",
            after_cursor=None,
            limit=50,
        )


def test_heartbeat_rejects_empty_account_identity() -> None:
    with pytest.raises(ValueError, match="heartbeat account_id is required"):
        Heartbeat(
            account_id="",
            ts=datetime(2026, 7, 29, tzinfo=timezone.utc),
            trading_state=TradingState.HALTED,
            readiness=False,
            projection_lag_ms=0,
            reconciliation_state=ReconciliationState.DEGRADED,
        )


def test_heartbeat_rejects_cross_account_identity() -> None:
    client = _client()

    with pytest.raises(ControlPlaneIdentityError, match="account_id mismatch"):
        client.heartbeat(
            "node-a",
            Heartbeat(
                account_id="account-b",
                ts=datetime(2026, 7, 29, tzinfo=timezone.utc),
                trading_state=TradingState.HALTED,
                readiness=False,
                projection_lag_ms=0,
                reconciliation_state=ReconciliationState.DEGRADED,
            ),
        )


def _client() -> HttpControlPlaneClient:
    return HttpControlPlaneClient(
        base_url="https://control-plane.invalid",
        token="node-a-token",
        node_id="node-a",
        account_id="account-a",
    )
