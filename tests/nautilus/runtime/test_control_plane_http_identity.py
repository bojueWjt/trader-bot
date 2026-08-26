from __future__ import annotations

import json
import socket
import sys
import time
from email.message import Message
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

import execution_domain.http_client as http_client_module  # noqa: E402
from execution_domain.contracts import ReconciliationState  # noqa: E402
from execution_domain.control_plane import (  # noqa: E402
    CommandAckStatus,
    Heartbeat,
    TradingState,
)
from execution_domain.http_client import (  # noqa: E402
    ControlPlaneConnectTimeout,
    ControlPlaneFencingError,
    ControlPlaneHttpError,
    ControlPlaneIdentityError,
    ControlPlaneReadTimeout,
    ControlPlaneTotalTimeout,
    HttpControlPlaneClient,
)

REDIS_FENCING_EPOCH = "11111111-1111-4111-8111-111111111111"
RUNTIME_GENERATION = "runtime-a"
LEASE_FENCING_TOKEN = 41


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
    assert request.get_header("X-redis-fencing-epoch") == REDIS_FENCING_EPOCH
    assert request.get_header("X-runtime-generation") == RUNTIME_GENERATION
    assert request.get_header("X-lease-fencing-token") == str(
        LEASE_FENCING_TOKEN
    )


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
            runtime_generation="runtime-a",
            lease_fencing_token=41,
            heartbeat_sequence=7,
            release_id="release-a",
            image_digest="sha256:" + ("1" * 64),
            config_sha256="2" * 64,
            dependency_lock_sha256="3" * 64,
            schema_epoch="2026-08-08",
            positions=(),
            regular_orders=(
                {
                    "client_order_id": "order-1",
                    "instrument_id": "BTCUSDT-PERP.BINANCE",
                },
            ),
            algo_orders=(),
            positions_snapshot_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
            regular_orders_snapshot_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
            algo_orders_snapshot_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
            reconciliation_completed_at=datetime(
                2026,
                7,
                29,
                tzinfo=timezone.utc,
            ),
            open_orders=(
                {
                    "client_order_id": "order-1",
                    "instrument_id": "BTCUSDT-PERP.BINANCE",
                },
            ),
        ),
    )

    command_query = parse_qs(urlparse(requests[0].full_url).query)
    heartbeat_body = json.loads(requests[1].data)
    assert command_query["account_id"] == ["account-a"]
    assert heartbeat_body["account_id"] == "account-a"
    assert heartbeat_body["runtime_generation"] == "runtime-a"
    assert heartbeat_body["lease_fencing_token"] == 41
    assert heartbeat_body["heartbeat_sequence"] == 7
    assert heartbeat_body["release_id"] == "release-a"
    assert heartbeat_body["image_digest"] == "sha256:" + ("1" * 64)
    assert heartbeat_body["config_sha256"] == "2" * 64
    assert heartbeat_body["dependency_lock_sha256"] == "3" * 64
    assert heartbeat_body["schema_epoch"] == "2026-08-08"
    assert heartbeat_body["positions"] == []
    assert heartbeat_body["regular_orders"] == [
        {
            "client_order_id": "order-1",
            "instrument_id": "BTCUSDT-PERP.BINANCE",
        }
    ]
    assert heartbeat_body["algo_orders"] == []
    assert heartbeat_body["positions_snapshot_at"] == "2026-07-29T00:00:00+00:00"
    assert heartbeat_body["regular_orders_snapshot_at"] == "2026-07-29T00:00:00+00:00"
    assert heartbeat_body["algo_orders_snapshot_at"] == "2026-07-29T00:00:00+00:00"
    assert (
        heartbeat_body["reconciliation_completed_at"]
        == "2026-07-29T00:00:00+00:00"
    )
    assert heartbeat_body["open_orders"] == [
        {
            "client_order_id": "order-1",
            "instrument_id": "BTCUSDT-PERP.BINANCE",
        }
    ]
    assert requests[1].get_header("X-account-id") == "account-a"


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


def test_open_order_pull_is_account_scoped_and_preserves_projection_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = []

    def urlopen(request, timeout):
        del timeout
        requests.append(request)
        return _Response(
            {
                "account_id": "account-a",
                "orders": [
                    {
                        "client_order_id": (
                            "B3562ddc2a0e74f509dc34253ab80beef01"
                        ),
                        "venue_order_id": "25082516000001",
                        "instrument_id": "PENGUUSDT-PERP.BINANCE",
                        "status": "accepted",
                        "side": "long",
                        "order_type": "LIMIT",
                        "position_side": "BOTH",
                        "time_in_force": "GTC",
                        "reduce_only": False,
                        "tags": [
                            "intent_id="
                            "3562ddc2-a0e7-4f50-9dc3-4253ab80beef"
                        ],
                    }
                ],
            }
        )

    monkeypatch.setattr(http_client_module, "urlopen", urlopen)
    client = _client()

    orders = client.fetch_open_orders("account-a")

    assert orders[0]["instrument_id"] == (
        "PENGUUSDT-PERP.BINANCE"
    )
    query = parse_qs(urlparse(requests[0].full_url).query)
    assert query == {
        "account_id": ["account-a"],
        "status": ["open"],
    }
    assert requests[0].get_header("X-node-id") == "node-a"
    assert requests[0].get_header("X-account-id") == "account-a"


def test_open_order_pull_rejects_cross_account_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_urlopen(request, timeout):
        del request, timeout
        raise AssertionError("network request must not be attempted")

    monkeypatch.setattr(
        http_client_module,
        "urlopen",
        unexpected_urlopen,
    )
    client = _client()

    with pytest.raises(
        ControlPlaneIdentityError,
        match="account_id mismatch",
    ):
        client.fetch_open_orders("account-b")


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


def test_command_ack_contract_preserves_running_state() -> None:
    assert CommandAckStatus.RUNNING.value == "running"


def test_connect_timeout_has_stable_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def urlopen(request, timeout):
        del request, timeout
        raise URLError(socket.timeout("connect timed out"))

    monkeypatch.setattr(http_client_module, "urlopen", urlopen)
    client = _client()

    with pytest.raises(ControlPlaneConnectTimeout, match="connect timeout"):
        client.fetch_intents("account-a", None, 10)


@pytest.mark.parametrize(
    "timeout_value",
    [0, -1, float("nan"), float("inf"), True],
)
def test_client_rejects_invalid_deadlines(timeout_value: object) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        _client(total_timeout_seconds=timeout_value)


def test_read_timeout_has_stable_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowResponse(_Response):
        def read(self) -> bytes:
            raise socket.timeout("read timed out")

    monkeypatch.setattr(
        http_client_module,
        "urlopen",
        lambda request, timeout: SlowResponse({}),
    )
    client = _client()

    with pytest.raises(ControlPlaneReadTimeout, match="read timeout"):
        client.fetch_intents("account-a", None, 10)


def test_total_deadline_bounds_a_permanently_blocked_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = Event()
    fatal_reasons: list[str] = []
    request_count = 0

    def urlopen(request, timeout):
        nonlocal request_count
        del request, timeout
        request_count += 1
        release.wait()
        return _Response({})

    monkeypatch.setattr(http_client_module, "urlopen", urlopen)
    client = _client(
        total_timeout_seconds=0.02,
        fatal_transport_hook=fatal_reasons.append,
    )

    started_at = time.monotonic()
    with pytest.raises(ControlPlaneTotalTimeout, match="total deadline"):
        client.fetch_intents("account-a", None, 10)
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.1
    assert fatal_reasons == [
        "GET /v1/nodes/node-a/intents?"
        "account_id=account-a&limit=10&wait_ms=0 "
        "total deadline exceeded after 0.020s"
    ]
    with pytest.raises(ControlPlaneTotalTimeout, match="total deadline"):
        client.fetch_intents("account-a", None, 10)
    assert request_count == 1
    release.set()


def test_stale_writer_rejection_triggers_injected_fatal_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fatal_reasons: list[str] = []
    request_count = 0
    headers = Message()
    headers["X-Writer-Fence-Rejected"] = "true"

    def urlopen(request, timeout):
        nonlocal request_count
        del request, timeout
        request_count += 1
        raise HTTPError(
            url="https://control-plane.invalid/v1/nodes/node-a/commands",
            code=409,
            msg="Conflict",
            hdrs=headers,
            fp=BytesIO(b'{"detail":"stale node writer"}'),
        )

    monkeypatch.setattr(http_client_module, "urlopen", urlopen)
    client = _client(fatal_fence_hook=fatal_reasons.append)

    with pytest.raises(ControlPlaneFencingError, match="stale node writer"):
        client.poll_commands("node-a", None)

    assert fatal_reasons == [
        "control-plane rejected stale writer: stale node writer"
    ]
    with pytest.raises(ControlPlaneFencingError, match="stale node writer"):
        client.fetch_intents("account-a", None, 10)
    assert request_count == 1


def test_fetch_intents_keeps_malformed_items_raw_for_per_item_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    malformed = {
        "schema_version": "9.9",
        "intent_id": "fae9d66e-8574-4580-9713-c92a7f7bf783",
    }
    valid = {
        "schema_version": "1.0",
        "intent_id": "3df4f122-5b12-4889-9334-bd2b230af01c",
    }
    monkeypatch.setattr(
        client,
        "_request_json",
        lambda *args, **kwargs: {
            "items": [
                {"cursor": "c1", "intent": malformed},
                {"cursor": "c2", "intent": valid},
            ],
            "next_cursor": "c2",
        },
    )

    batch = client.fetch_intents("account-a", None, 10)

    assert [item.cursor for item in batch.items] == ["c1", "c2"]
    assert batch.items[0].intent == malformed
    assert batch.items[1].intent == valid


def _client(**overrides) -> HttpControlPlaneClient:
    values = {
        "base_url": "https://control-plane.invalid",
        "token": "node-a-token",
        "node_id": "node-a",
        "account_id": "account-a",
        "redis_fencing_epoch": REDIS_FENCING_EPOCH,
        "runtime_generation": RUNTIME_GENERATION,
        "lease_fencing_token": LEASE_FENCING_TOKEN,
    }
    values.update(overrides)
    return HttpControlPlaneClient(
        **values,
    )


def test_missing_heartbeat_writer_rejection_is_retryable_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "node writer heartbeat is missing" can only occur before this
    # node's first accepted heartbeat (the row does not exist yet), so a
    # transient evidence gap on the opening heartbeat must surface as a
    # retryable HTTP error instead of fencing the process into a
    # bootstrap crash loop.
    fatal_reasons: list[str] = []
    headers = Message()
    headers["X-Writer-Fence-Rejected"] = "true"

    def urlopen(request, timeout):
        del request, timeout
        raise HTTPError(
            url="https://control-plane.invalid/v1/nodes/node-a/commands",
            code=409,
            msg="Conflict",
            hdrs=headers,
            fp=BytesIO(b'{"detail":"node writer heartbeat is missing"}'),
        )

    monkeypatch.setattr(http_client_module, "urlopen", urlopen)
    client = _client(fatal_fence_hook=fatal_reasons.append)

    with pytest.raises(
        ControlPlaneHttpError,
        match="node writer heartbeat is missing",
    ):
        client.poll_commands("node-a", None)

    assert fatal_reasons == []
    # The client is NOT latched: later calls still reach the server.
    with pytest.raises(
        ControlPlaneHttpError,
        match="node writer heartbeat is missing",
    ):
        client.poll_commands("node-a", None)
