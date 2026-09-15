"""Real API/SQL wiring for add preconditions and transactional close barriers."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import read_api
from test_operator_add_position import (
    ACCOUNT_B,
    NODE_B,
    REDIS_FENCING_EPOCH,
    RUNTIME_GENERATION,
    SYMBOL,
    _connect,
    _entry_body,
    _headers,
    _post,
    _same_side_books,
    client,  # noqa: F401 -- shared migrated-Postgres API fixture
)


@pytest.fixture()
def revision_client(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv(
        "NAUTILUS_NODE_AUTH_JSON",
        json.dumps({NODE_B: {"account_id": ACCOUNT_B, "token": "revision-node-token"}}),
    )
    return client


def _revision(account_revision: int = 0, book_revision: int = 0) -> dict:
    return {
        "account_id": ACCOUNT_B,
        "instrument_id": SYMBOL,
        "position_side": "LONG",
        "account_revision": account_revision,
        "book_revision": book_revision,
    }


def _close_body(client_ref: str) -> dict:
    return {
        "action": "close_position", "account_id": ACCOUNT_B,
        "symbol": SYMBOL, "position_side": "long",
        "reason": "position revision integration", "client_ref": client_ref,
    }


def _close_all(client: TestClient, request_id: str):
    return client.post(
        "/v1/commands", headers=_headers(request_id),
        json={
            "type": "CLOSE_ALL", "confirm": True,
            "reason": "position revision integration",
            "scope": {"account_id": ACCOUNT_B}, "target_nodes": [NODE_B],
        },
    )


def _node_intents(client: TestClient) -> list[dict]:
    response = client.get(
        f"/v1/nodes/{NODE_B}/intents", params={"account_id": ACCOUNT_B},
        headers={
            "Authorization": "Bearer revision-node-token",
            "X-Node-Id": NODE_B, "X-Account-Id": ACCOUNT_B,
            "X-Redis-Fencing-Epoch": REDIS_FENCING_EPOCH,
            "X-Runtime-Generation": RUNTIME_GENERATION,
            "X-Lease-Fencing-Token": "41",
        },
    )
    assert response.status_code == 200, response.text
    return [item["intent"] for item in response.json()["items"]]


def _rows(url: str, sql: str, params: tuple = ()) -> list:
    with _connect(url) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def test_add_api_persists_precondition_and_node_poll_preserves_both_versions(
    revision_client: TestClient, migrated_db: str,
) -> None:
    _same_side_books(migrated_db)
    body = _entry_body("add_position", "revision-first-add")
    first = _post(revision_client, body)
    assert first.status_code == 200, first.text
    intent_id = first.json()["intent_id"]
    assert first.json()["order_plan"]["execution_precondition"] == _revision()
    stored = _rows(migrated_db, "SELECT order_plan FROM trade_intents WHERE intent_id=%s", (intent_id,))[0][0]
    assert stored["execution_precondition"] == _revision()
    assert "execution_revision" not in stored
    intents = _node_intents(revision_client)
    fetched = next(intent for intent in intents if intent["intent_id"] == intent_id)
    assert fetched["order_plan"]["execution_precondition"] == _revision()
    assert fetched["order_plan"]["execution_revision"] == _revision()
    retry = _post(revision_client, body)
    assert retry.status_code == 200, retry.text
    assert retry.json()["intent_id"] == intent_id
    assert retry.json()["replay"] is True


def test_close_api_advances_book_once_and_old_add_poll_gets_new_current_revision(
    revision_client: TestClient, migrated_db: str,
) -> None:
    _same_side_books(migrated_db)
    added = _post(revision_client, _entry_body("add_position", "revision-before-close"))
    assert added.status_code == 200, added.text
    add_id = added.json()["intent_id"]
    close_body = _close_body("revision-close")
    closed = _post(revision_client, close_body)
    assert closed.status_code == 200, closed.text
    close_id = closed.json()["intent_id"]
    retry = _post(revision_client, close_body)
    assert retry.status_code == 200, retry.text
    assert retry.json()["intent_id"] == close_id
    assert retry.json()["replay"] is True
    assert _rows(migrated_db, "SELECT account_id, instrument_id, position_side, revision FROM position_revisions") == [
        (ACCOUNT_B, SYMBOL, "LONG", 1),
    ]
    assert _rows(migrated_db, "SELECT operation_id FROM position_revision_invalidations") == [(close_id,)]
    fetched = next(intent for intent in _node_intents(revision_client) if intent["intent_id"] == add_id)
    assert fetched["order_plan"]["execution_precondition"] == _revision()
    assert fetched["order_plan"]["execution_revision"] == _revision(book_revision=1)
    # Equal venue quantity after reopen must not rewind the semantic revision.
    _same_side_books(migrated_db, quantity="1")
    new_add = _post(revision_client, _entry_body("add_position", "revision-after-reopen"))
    assert new_add.status_code == 200, new_add.text
    assert new_add.json()["order_plan"]["execution_precondition"] == _revision(book_revision=1)
    original = _rows(migrated_db, "SELECT order_plan FROM trade_intents WHERE intent_id=%s", (add_id,))[0][0]
    assert original["execution_precondition"] == _revision()
    assert "execution_revision" not in original


def test_single_position_close_without_side_preserves_legacy_resolution(
    revision_client: TestClient, migrated_db: str,
) -> None:
    _same_side_books(migrated_db)
    opened = _post(revision_client, _entry_body("open_position", "revision-parent-no-side"))
    assert opened.status_code == 200, opened.text
    body = _close_body("revision-close-no-side")
    body.pop("position_side")
    body.update(opened.json()["order_plan"]["authorization"])
    body["source"] = "dashboard"
    body["created_by_service"] = "dashboard"
    body["parent_intent_id"] = opened.json()["intent_id"]
    response = _post(revision_client, body)
    assert response.status_code == 200, response.text
    assert _rows(migrated_db, "SELECT instrument_id, position_side, revision FROM position_revisions") == [
        ("*", "*", 1),
    ]
    retry = _post(revision_client, body)
    assert retry.status_code == 200, retry.text
    assert retry.json()["intent_id"] == response.json()["intent_id"]


def test_close_all_api_advances_account_once_with_command_and_node_delivery(
    revision_client: TestClient, migrated_db: str,
) -> None:
    _same_side_books(migrated_db)
    added = _post(revision_client, _entry_body("add_position", "revision-before-close-all"))
    assert added.status_code == 200, added.text
    first = _close_all(revision_client, "revision-close-all")
    assert first.status_code == 200, first.text
    command_id = first.json()["command_id"]
    retry = _close_all(revision_client, "revision-close-all")
    assert retry.status_code == 200, retry.text
    assert retry.json()["command_id"] == command_id
    assert retry.json()["idempotent"] is True
    assert _rows(migrated_db, "SELECT account_id, instrument_id, position_side, revision FROM position_revisions") == [
        (ACCOUNT_B, "*", "*", 1),
    ]
    assert _rows(migrated_db, "SELECT operation_id FROM position_revision_invalidations") == [(command_id,)]
    assert _rows(migrated_db, "SELECT command_type::text FROM operator_commands WHERE command_id=%s", (command_id,)) == [("CLOSE_ALL",)]
    assert _rows(migrated_db, "SELECT node_id FROM command_node_acks WHERE command_id=%s", (command_id,)) == [(NODE_B,)]
    fetched = next(intent for intent in _node_intents(revision_client) if intent["intent_id"] == added.json()["intent_id"])
    assert fetched["order_plan"]["execution_precondition"] == _revision()
    assert fetched["order_plan"]["execution_revision"] == _revision(account_revision=1)


@pytest.mark.parametrize("close_kind", ["close_position", "close_all"])
def test_failure_after_revision_write_rolls_back_close_and_revision_together(
    revision_client: TestClient, migrated_db: str,
    monkeypatch: pytest.MonkeyPatch, close_kind: str,
) -> None:
    _same_side_books(migrated_db)
    name = "invalidate_book" if close_kind == "close_position" else "invalidate_account"
    original = getattr(read_api.position_revision, name)
    observed_revision = []

    def write_then_fail(cur, *args, **kwargs):
        original(cur, *args, **kwargs)
        cur.execute("SELECT revision FROM position_revisions WHERE account_id=%s", (ACCOUNT_B,))
        observed_revision.extend(cur.fetchall())
        raise RuntimeError("injected failure after durable revision SQL")

    monkeypatch.setattr(read_api.position_revision, name, write_then_fail)
    with pytest.raises(RuntimeError, match="injected failure after durable revision SQL"):
        if close_kind == "close_position":
            _post(revision_client, _close_body(f"rollback-{uuid4()}"))
        else:
            _close_all(revision_client, f"rollback-{uuid4()}")
    assert observed_revision == [(1,)]
    assert _rows(migrated_db, "SELECT revision FROM position_revisions") == []
    assert _rows(migrated_db, "SELECT operation_id FROM position_revision_invalidations") == []
    assert _rows(migrated_db, "SELECT intent_id FROM trade_intents") == []
    assert _rows(migrated_db, "SELECT command_id FROM operator_commands") == []
    assert _rows(migrated_db, "SELECT command_id FROM command_node_acks") == []
