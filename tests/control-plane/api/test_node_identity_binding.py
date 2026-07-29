from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

import read_api


NODE_A = "nautilus-node-account-a"
ACCOUNT_A = "account-a"
TOKEN_A = "node-a-token"


def _authorization(token: str = TOKEN_A) -> str:
    return f"Bearer {token}"


def _binding_payload(
    *,
    token_a: str = TOKEN_A,
    token_b: str = "node-b-token",
) -> str:
    return json.dumps(
        {
            NODE_A: {
                "account_id": ACCOUNT_A,
                "token": token_a,
            },
            "nautilus-node-account-b": {
                "account_id": "account-b",
                "token": token_b,
            },
        }
    )


def test_strict_node_identity_accepts_exact_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAUTILUS_NODE_AUTH_JSON", _binding_payload())

    account_id = read_api.require_node(
        _authorization(),
        node_id=NODE_A,
        account_id=ACCOUNT_A,
        x_node_id=NODE_A,
        x_account_id=ACCOUNT_A,
    )

    assert account_id == ACCOUNT_A


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("node_id", "nautilus-node-account-b"),
        ("account_id", "account-b"),
        ("x_node_id", "nautilus-node-account-b"),
        ("x_account_id", "account-b"),
    ),
)
def test_strict_node_identity_rejects_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    monkeypatch.setenv("NAUTILUS_NODE_AUTH_JSON", _binding_payload())
    request = {
        "node_id": NODE_A,
        "account_id": ACCOUNT_A,
        "x_node_id": NODE_A,
        "x_account_id": ACCOUNT_A,
    }
    request[field] = value

    with pytest.raises(HTTPException) as exc_info:
        read_api.require_node(_authorization(), **request)

    assert exc_info.value.status_code == 403


def test_strict_node_identity_rejects_wrong_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAUTILUS_NODE_AUTH_JSON", _binding_payload())

    with pytest.raises(HTTPException) as exc_info:
        read_api.require_node(
            _authorization("wrong-token"),
            node_id=NODE_A,
            account_id=ACCOUNT_A,
            x_node_id=NODE_A,
            x_account_id=ACCOUNT_A,
        )

    assert exc_info.value.status_code == 401


def test_strict_node_identity_rejects_duplicate_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "NAUTILUS_NODE_AUTH_JSON",
        _binding_payload(token_a="shared-token", token_b="shared-token"),
    )

    with pytest.raises(HTTPException) as exc_info:
        read_api.require_node(
            _authorization("shared-token"),
            node_id=NODE_A,
            account_id=ACCOUNT_A,
            x_node_id=NODE_A,
            x_account_id=ACCOUNT_A,
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "node auth tokens must be unique"
