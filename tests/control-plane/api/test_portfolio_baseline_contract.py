from __future__ import annotations

import pytest
from fastapi import HTTPException

import read_api
from execution_domain.control_plane import portfolio_baseline_sha256


def test_control_plane_portfolio_baseline_matches_shared_contract() -> None:
    heartbeat = {
        "positions": [
            {
                "symbol": "XAUUSDT",
                "quantity": "1.00",
                "mark_price": "2400",
            }
        ],
        "regular_orders": [
            {
                "symbol": "ETHUSDT",
                "client_order_id": "eth-order",
                "quantity": "0.10",
                "price": "3000.00",
            }
        ],
        "algo_orders": [],
    }

    assert read_api._portfolio_baseline_sha256(
        heartbeat,
        "SOLUSDT",
    ) == portfolio_baseline_sha256(
        heartbeat,
        "SOLUSDT",
    )


def test_control_plane_portfolio_baseline_delegates_to_shared_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heartbeat = {
        "positions": [],
        "regular_orders": [],
        "algo_orders": [],
    }
    calls = []

    def shared_hash(candidate: dict, target_symbol: str) -> str:
        calls.append((candidate, target_symbol))
        return "a" * 64

    monkeypatch.setattr(read_api, "portfolio_baseline_sha256", shared_hash)

    assert read_api._portfolio_baseline_sha256(
        heartbeat,
        "SOLUSDT",
    ) == "a" * 64
    assert calls == [(heartbeat, "SOLUSDT")]


@pytest.mark.parametrize(
    "heartbeat",
    (
        {
            "positions": False,
            "regular_orders": [],
            "algo_orders": [],
        },
        {
            "positions": [],
            "regular_orders": ["invalid-row"],
            "algo_orders": [],
        },
    ),
)
def test_control_plane_preserves_invalid_evidence_http_contract(
    heartbeat: dict,
) -> None:
    with pytest.raises(HTTPException) as exc_info:
        read_api._portfolio_baseline_sha256(heartbeat, "SOLUSDT")

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "node exchange evidence is invalid"
