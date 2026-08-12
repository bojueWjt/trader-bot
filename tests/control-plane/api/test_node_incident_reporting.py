from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import pytest
from fastapi.testclient import TestClient

import read_api
from app_roles import AppRole

REPO_ROOT = Path(__file__).resolve().parents[3]
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
if str(EXECUTION_DOMAIN_ROOT) not in sys.path:
    sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from execution_domain.control_plane import (  # noqa: E402
    IncidentSeverity,
    ProductionIncidentReport,
    ProductionIncidentSink,
)
from execution_domain.http_client import (  # noqa: E402
    ControlPlaneIdentityError,
    HttpControlPlaneClient,
)
from execution_domain.testing import InMemoryControlPlane  # noqa: E402


ACCOUNT_A = "account-a"
NODE_A = "nautilus-node-account-a"
NODE_A_TOKEN = "node-a-token"
REASON = "redis_capacity_halt"


@pytest.fixture()
def node_control_client(
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: str,
) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setenv(
        "NAUTILUS_NODE_AUTH_JSON",
        json.dumps(
            {
                NODE_A: {
                    "account_id": ACCOUNT_A,
                    "token": NODE_A_TOKEN,
                }
            }
        ),
    )
    return TestClient(read_api.create_app(AppRole.NODE_CONTROL))


def _node_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {NODE_A_TOKEN}",
        "X-Node-Id": NODE_A,
        "X-Account-Id": ACCOUNT_A,
    }


def test_repeated_open_incident_is_deduplicated_and_escalated(
    node_control_client: TestClient,
    migrated_db: str,
) -> None:
    first = node_control_client.post(
        f"/v1/nodes/{NODE_A}/incidents",
        headers=_node_headers(),
        json={
            "account_id": ACCOUNT_A,
            "severity": "P2",
            "reason": REASON,
            "summary": "Redis capacity guard entered sticky halt.",
        },
    )
    repeated = node_control_client.post(
        f"/v1/nodes/{NODE_A}/incidents",
        headers=_node_headers(),
        json={
            "account_id": ACCOUNT_A,
            "severity": "P1",
            "reason": REASON,
            "summary": "Redis pressure persisted for 30 seconds.",
        },
    )

    assert first.status_code == 200
    assert repeated.status_code == 200
    first_receipt = first.json()
    repeated_receipt = repeated.json()
    assert first_receipt == {
        "incident_id": first_receipt["incident_id"],
        "account_id": ACCOUNT_A,
        "node_id": NODE_A,
        "reason": REASON,
        "severity": "P2",
        "status": "open",
        "summary": "Redis capacity guard entered sticky halt.",
        "opened_at": first_receipt["opened_at"],
        "deduplicated": False,
    }
    assert repeated_receipt == {
        "incident_id": first_receipt["incident_id"],
        "account_id": ACCOUNT_A,
        "node_id": NODE_A,
        "reason": REASON,
        "severity": "P1",
        "status": "open",
        "summary": "Redis pressure persisted for 30 seconds.",
        "opened_at": first_receipt["opened_at"],
        "deduplicated": True,
    }

    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT incident_id, severity, status, summary
            FROM production_incidents
            WHERE account_id=%s
            """,
            (ACCOUNT_A,),
        )
        rows = cur.fetchall()

    assert len(rows) == 1
    assert str(rows[0][0]) == first_receipt["incident_id"]
    assert rows[0][1:3] == ("P1", "open")
    assert f"node={NODE_A}" in rows[0][3]
    assert f"reason={REASON}" in rows[0][3]
    assert rows[0][3].endswith(
        "Redis pressure persisted for 30 seconds."
    )


def test_concurrent_repeated_reports_create_one_open_incident(
    node_control_client: TestClient,
    migrated_db: str,
) -> None:
    def report(index: int):
        return node_control_client.post(
            f"/v1/nodes/{NODE_A}/incidents",
            headers=_node_headers(),
            json={
                "account_id": ACCOUNT_A,
                "severity": "P1",
                "reason": REASON,
                "summary": f"Concurrent report {index}.",
            },
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(report, range(8)))

    assert {response.status_code for response in responses} == {200}
    receipts = [response.json() for response in responses]
    assert len({receipt["incident_id"] for receipt in receipts}) == 1
    assert sum(not receipt["deduplicated"] for receipt in receipts) == 1

    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*)
            FROM production_incidents
            WHERE account_id=%s
              AND status='open'
            """,
            (ACCOUNT_A,),
        )
        open_count = cur.fetchone()[0]

    assert open_count == 1


def test_closed_incident_allows_a_new_incident_for_the_same_reason(
    node_control_client: TestClient,
    migrated_db: str,
) -> None:
    first = node_control_client.post(
        f"/v1/nodes/{NODE_A}/incidents",
        headers=_node_headers(),
        json={
            "account_id": ACCOUNT_A,
            "severity": "P1",
            "reason": REASON,
            "summary": "Initial halt.",
        },
    )
    assert first.status_code == 200

    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE production_incidents
            SET status='closed',
                closed_at=now()
            WHERE incident_id=%s
            """,
            (first.json()["incident_id"],),
        )

    reopened = node_control_client.post(
        f"/v1/nodes/{NODE_A}/incidents",
        headers=_node_headers(),
        json={
            "account_id": ACCOUNT_A,
            "severity": "P1",
            "reason": REASON,
            "summary": "A later independent halt.",
        },
    )

    assert reopened.status_code == 200
    assert reopened.json()["incident_id"] != first.json()["incident_id"]
    assert reopened.json()["deduplicated"] is False


def test_incident_report_rejects_cross_account_identity_before_write(
    node_control_client: TestClient,
    migrated_db: str,
) -> None:
    response = node_control_client.post(
        f"/v1/nodes/{NODE_A}/incidents",
        headers=_node_headers(),
        json={
            "account_id": "account-b",
            "severity": "P1",
            "reason": REASON,
            "summary": "Spoofed incident.",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "node account mismatch"
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM production_incidents")
        incident_count = cur.fetchone()[0]
    assert incident_count == 0


def test_incident_report_requires_node_account_binding_configuration(
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: str,
) -> None:
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.delenv("NAUTILUS_NODE_AUTH_JSON", raising=False)
    monkeypatch.setenv("NAUTILUS_NODE_TOKEN", NODE_A_TOKEN)
    client = TestClient(read_api.create_app(AppRole.NODE_CONTROL))

    response = client.post(
        f"/v1/nodes/{NODE_A}/incidents",
        headers=_node_headers(),
        json={
            "account_id": ACCOUNT_A,
            "severity": "P1",
            "reason": REASON,
            "summary": "Shared-token report.",
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "node identity bindings are required for incident reporting"
    )


def test_incident_route_is_owned_only_by_node_control() -> None:
    path = f"/v1/nodes/{{node_id}}/incidents"
    node_control_paths = {
        route.path
        for route in read_api.create_app(AppRole.NODE_CONTROL).routes
    }
    event_ingest_paths = {
        route.path
        for route in read_api.create_app(AppRole.EVENT_INGEST).routes
    }
    operator_query_paths = {
        route.path
        for route in read_api.create_app(AppRole.OPERATOR_QUERY).routes
    }

    assert path in node_control_paths
    assert path not in event_ingest_paths
    assert path not in operator_query_paths


def test_http_client_reports_incident_and_parses_deduplication_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = HttpControlPlaneClient(
        base_url="https://control-plane.invalid",
        token=NODE_A_TOKEN,
        node_id=NODE_A,
        account_id=ACCOUNT_A,
    )
    requests = []

    def request_json(method, path, body=None, allow_empty=False):
        requests.append((method, path, body, allow_empty))
        return {
            "incident_id": "6206c1b4-432d-4c4e-87f0-52e1cfb75fc6",
            "account_id": ACCOUNT_A,
            "node_id": NODE_A,
            "reason": REASON,
            "severity": "P1",
            "status": "open",
            "summary": "Redis pressure persisted for 30 seconds.",
            "opened_at": "2026-08-08T18:03:27+00:00",
            "deduplicated": True,
        }

    monkeypatch.setattr(client, "_request_json", request_json)
    receipt = client.report_incident(
        NODE_A,
        ProductionIncidentReport(
            account_id=ACCOUNT_A,
            severity=IncidentSeverity.P1,
            reason=REASON,
            summary="Redis pressure persisted for 30 seconds.",
        ),
    )

    assert requests == [
        (
            "POST",
            f"/v1/nodes/{NODE_A}/incidents",
            {
                "account_id": ACCOUNT_A,
                "severity": "P1",
                "reason": REASON,
                "summary": "Redis pressure persisted for 30 seconds.",
            },
            False,
        )
    ]
    assert receipt.incident_id == "6206c1b4-432d-4c4e-87f0-52e1cfb75fc6"
    assert receipt.account_id == ACCOUNT_A
    assert receipt.node_id == NODE_A
    assert receipt.reason == REASON
    assert receipt.severity is IncidentSeverity.P1
    assert receipt.status == "open"
    assert receipt.summary == "Redis pressure persisted for 30 seconds."
    assert receipt.opened_at == datetime(
        2026,
        8,
        8,
        18,
        3,
        27,
        tzinfo=timezone.utc,
    )
    assert receipt.deduplicated is True


def test_http_client_rejects_cross_account_incident_before_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = HttpControlPlaneClient(
        base_url="https://control-plane.invalid",
        token=NODE_A_TOKEN,
        node_id=NODE_A,
        account_id=ACCOUNT_A,
    )
    monkeypatch.setattr(
        client,
        "_request_json",
        lambda *args, **kwargs: pytest.fail("request must not be sent"),
    )

    with pytest.raises(ControlPlaneIdentityError):
        client.report_incident(
            NODE_A,
            ProductionIncidentReport(
                account_id="account-b",
                severity=IncidentSeverity.P1,
                reason=REASON,
                summary="Spoofed incident.",
            ),
        )


def test_incident_sink_is_an_explicit_client_capability() -> None:
    http_client = HttpControlPlaneClient(
        base_url="https://control-plane.invalid",
        token=NODE_A_TOKEN,
        node_id=NODE_A,
        account_id=ACCOUNT_A,
    )

    assert isinstance(http_client, ProductionIncidentSink)
    assert not isinstance(InMemoryControlPlane(), ProductionIncidentSink)
