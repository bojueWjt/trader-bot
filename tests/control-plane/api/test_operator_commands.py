from __future__ import annotations

import json

from fastapi.testclient import TestClient

import read_api

read_api._cp_paths()

import commands  # noqa: E402


NODE_ID = "nautilus-node-account-a"
ACCOUNT_ID = "account-a"
TOKEN = "node-a-token"


def test_command_poll_excludes_expired_resume_and_keeps_risk_reduction(
    db_conn,
    migrated_db,
    monkeypatch,
) -> None:
    expired_resume = _issue_command(
        db_conn,
        command_type="RESUME",
        key="expired-resume",
        timeout=-5,
    )
    active_resume = _issue_command(
        db_conn,
        command_type="RESUME",
        key="active-resume",
        timeout=30,
    )
    expired_halt = _issue_command(
        db_conn,
        command_type="HALT",
        key="expired-halt",
        timeout=-5,
    )
    expired_reduce = _issue_command(
        db_conn,
        command_type="REDUCE",
        key="expired-reduce",
        timeout=-5,
    )
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setenv(
        "NAUTILUS_NODE_AUTH_JSON",
        json.dumps(
            {
                NODE_ID: {
                    "account_id": ACCOUNT_ID,
                    "token": TOKEN,
                }
            }
        ),
    )

    response = TestClient(read_api.app).get(
        f"/v1/nodes/{NODE_ID}/commands",
        params={"account_id": ACCOUNT_ID},
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "X-Node-Id": NODE_ID,
            "X-Account-Id": ACCOUNT_ID,
        },
    )

    assert response.status_code == 200
    returned = {
        item["command_id"]: item
        for item in response.json()["commands"]
    }
    assert expired_resume["command_id"] not in returned
    assert active_resume["command_id"] in returned
    assert expired_halt["command_id"] in returned
    assert expired_reduce["command_id"] in returned
    assert returned[active_resume["command_id"]]["args"][
        "command_expires_at"
    ]


def _issue_command(
    conn,
    *,
    command_type: str,
    key: str,
    timeout: int,
) -> dict:
    return commands.issue_command(
        conn,
        command_type=command_type,
        requested_by="operator-1",
        reason="operator lifecycle command",
        idempotency_key=key,
        target_nodes=[NODE_ID],
        scope={
            "account_id": ACCOUNT_ID,
            "instruments": ["SOLUSDT"],
        },
        ack_timeout_seconds=timeout,
    )
