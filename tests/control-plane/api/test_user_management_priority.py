"""Explicit operator management does not inherit signal-channel restrictions."""
from uuid import uuid4

import pytest

import read_api
from test_operator_add_position import _headers, _same_side_books, client


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("context", ["none", "old_channel", "old_parent", "single_book_no_side"])
def test_user_management_ignores_signal_context(client, migrated_db, monkeypatch, dry_run, context):
    _same_side_books(migrated_db)
    def forbidden_lookup(*args, **kwargs):
        raise AssertionError("user order must not require a channel parent")
    monkeypatch.setattr(read_api, "_resolve_attribution", forbidden_lookup)
    monkeypatch.setattr(read_api, "_authorized_parent", forbidden_lookup)
    body = {
        "action": "partial_close", "symbol": "ATOMUSDT", "account_id": "account-b",
        "position_side": "long", "quantity": "0.1", "authorized_by_type": "user",
        "reason": "user explicitly reduces this position", "client_ref": "user-reduce-1",
        "dry_run": dry_run,
    }
    if context == "old_channel":
        body.update(channel="-100old", entry_ref="unknown-old-entry")
    elif context == "old_parent":
        body.update(parent_intent_id=str(uuid4()), created_by_service="hermes-agent", source="hermes-agent")
    elif context == "single_book_no_side":
        body.pop("position_side")
    response = client.post("/v1/operator/orders", headers=_headers("user-reduce-1"), json=body)
    assert response.status_code == 200, response.text
    data = response.json()
    if dry_run:
        assert data["authorization"]["authorized_by_type"] == "user"
        assert data["authorization"]["parent_intent_id"] is False
        assert data["attribution"]["would_reject"] is False
    else:
        for key in ("channel", "entry_ref", "parent_intent_id"):
            body.pop(key, None)
        replay = client.post("/v1/operator/orders", headers=_headers("user-reduce-1"), json=body)
        assert replay.status_code == 200, replay.text
        assert replay.json()["intent_id"] == data["intent_id"]


@pytest.mark.parametrize("state,presence", [
    ("known", {"long": True, "short": True}),
    ("stale", {"long": True, "short": False}),
])
def test_user_management_without_side_requires_a_unique_current_book(client, migrated_db, monkeypatch, state, presence):
    _same_side_books(migrated_db)
    monkeypatch.setattr(read_api, "_load_entry_venue_view", lambda *args, **kwargs: {
        "state": state, "presence": presence,
    })
    response = client.post("/v1/operator/orders", headers=_headers("user-no-side"), json={
        "action": "partial_close", "symbol": "ATOMUSDT", "account_id": "account-b",
        "quantity": "0.1", "authorized_by_type": "user",
        "reason": "reduce position", "client_ref": "user-no-side", "dry_run": True,
    })
    assert response.status_code == 400, response.text
    assert "pass position_side" in response.json()["detail"]


def test_internal_derived_order_still_requires_its_parent():
    with pytest.raises(read_api.HTTPException, match="parent_intent_id"):
        read_api._order_authorization(
            {"authorized_by_type": "user", "created_by_service": "protection-watchdog"},
            "unused", "account-b", "ATOMUSDT", "repair", "protection-watchdog", "operator", "req", "ref",
        )
