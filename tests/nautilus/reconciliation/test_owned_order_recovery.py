from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from runtime.owned_order_recovery import (  # noqa: E402
    BinanceOwnedOrderReconciler,
    OwnedOrderRecoveryError,
)


CLIENT_ORDER_ID = "B3562ddc2a0e74f509dc34253ab80beef01"


def test_penguusdt_fill_is_recovered_from_exact_order_and_user_trades() -> None:
    transport = RecordingTransport(
        {
            ("GET", "/fapi/v1/order"): {
                "symbol": "PENGUUSDT",
                "orderId": 25082516000001,
                "clientOrderId": CLIENT_ORDER_ID,
                "status": "FILLED",
                "side": "BUY",
                "positionSide": "BOTH",
                "type": "LIMIT",
                "timeInForce": "GTC",
                "origQty": "33300",
                "executedQty": "33300",
                "price": "0.009009",
                "avgPrice": "0.009009",
                "reduceOnly": False,
                "updateTime": 1787644801234,
            },
            ("GET", "/fapi/v1/userTrades"): [
                {
                    "symbol": "PENGUUSDT",
                    "orderId": 25082516000001,
                    "id": 925081600001,
                    "price": "0.009009",
                    "qty": "33300",
                    "quoteQty": "299.9997",
                    "commission": "0.11999988",
                    "commissionAsset": "USDT",
                    "realizedPnl": "0",
                    "side": "BUY",
                    "positionSide": "BOTH",
                    "buyer": True,
                    "maker": True,
                    "time": 1787644801000,
                }
            ],
        }
    )
    reconciler = BinanceOwnedOrderReconciler(transport=transport)
    candidate = SimpleNamespace(
        client_order_id=CLIENT_ORDER_ID,
        venue_order_id="25082516000001",
        instrument_id="PENGUUSDT-PERP.BINANCE",
        status="ACCEPTED",
        side="BUY",
        position_side="BOTH",
        order_type="LIMIT",
        time_in_force="GTC",
        reduce_only=False,
        tags=(
            "intent_id=3562ddc2-a0e7-4f50-9dc3-4253ab80beef",
        ),
    )

    captured = reconciler.capture((candidate,))
    events = reconciler.recover(captured)

    assert [
        (call[0], call[1], call[2])
        for call in transport.calls
    ] == [
        (
            "GET",
            "/fapi/v1/order",
            {
                "symbol": "PENGUUSDT",
                "orderId": "25082516000001",
            },
        ),
        (
            "GET",
            "/fapi/v1/userTrades",
            {
                "symbol": "PENGUUSDT",
                "orderId": "25082516000001",
                "limit": 1000,
            },
        ),
    ]
    assert len(events) == 1
    fill = events[0]
    assert fill.event_type == "OrderFilled"
    assert fill.client_order_id == CLIENT_ORDER_ID
    assert fill.venue_order_id == "25082516000001"
    assert fill.trade_id == "925081600001"
    assert fill.instrument_id == "PENGUUSDT-PERP.BINANCE"
    assert fill.last_qty == "33300"
    assert fill.last_px == "0.009009"
    assert fill.filled_qty == "33300"
    assert fill.leaves_qty == "0"
    assert fill.commission == "0.11999988"
    assert fill.commission_asset == "USDT"
    assert fill.recovered is True
    assert fill.source == "exchange_reconciliation"
    assert fill.ts_event == 1787644801000 * 1_000_000


def test_order_without_local_venue_id_uses_client_id_then_trade_order_id() -> None:
    transport = RecordingTransport(
        {
            ("GET", "/fapi/v1/order"): {
                "symbol": "PENGUUSDT",
                "orderId": 77,
                "clientOrderId": CLIENT_ORDER_ID,
                "status": "NEW",
                "side": "BUY",
                "positionSide": "BOTH",
                "type": "LIMIT",
                "timeInForce": "GTC",
                "origQty": "33300",
                "executedQty": "0",
                "price": "0.009009",
                "avgPrice": "0",
                "reduceOnly": False,
                "updateTime": 1787644801234,
            },
            ("GET", "/fapi/v1/userTrades"): [],
        }
    )
    reconciler = BinanceOwnedOrderReconciler(transport=transport)
    order = SimpleNamespace(
        client_order_id=CLIENT_ORDER_ID,
        venue_order_id="",
        instrument_id="PENGUUSDT-PERP.BINANCE",
        status="SUBMITTED",
    )

    events = reconciler.recover(reconciler.capture((order,)))

    assert events == ()
    assert transport.calls[0][2] == {
        "symbol": "PENGUUSDT",
        "origClientOrderId": CLIENT_ORDER_ID,
    }
    assert transport.calls[1][2]["orderId"] == "77"


def test_canceled_partial_fill_recovers_fill_before_terminal_event() -> None:
    transport = RecordingTransport(
        {
            ("GET", "/fapi/v1/order"): {
                "symbol": "PENGUUSDT",
                "orderId": 88,
                "clientOrderId": CLIENT_ORDER_ID,
                "status": "CANCELED",
                "side": "BUY",
                "positionSide": "BOTH",
                "type": "LIMIT",
                "timeInForce": "GTC",
                "origQty": "37462",
                "executedQty": "33300",
                "price": "0.009009",
                "avgPrice": "0.009009",
                "reduceOnly": False,
                "updateTime": 1787644801234,
            },
            ("GET", "/fapi/v1/userTrades"): [
                {
                    "symbol": "PENGUUSDT",
                    "orderId": 88,
                    "id": 99,
                    "price": "0.009009",
                    "qty": "33300",
                    "quoteQty": "299.9997",
                    "commission": "0.11999988",
                    "commissionAsset": "USDT",
                    "realizedPnl": "0",
                    "side": "BUY",
                    "positionSide": "BOTH",
                    "time": 1787644801000,
                }
            ],
        }
    )
    reconciler = BinanceOwnedOrderReconciler(transport=transport)
    order = SimpleNamespace(
        client_order_id=CLIENT_ORDER_ID,
        venue_order_id="88",
        instrument_id="PENGUUSDT-PERP.BINANCE",
        status="ACCEPTED",
    )

    events = reconciler.recover(reconciler.capture((order,)))

    assert [event.event_type for event in events] == [
        "OrderFilled",
        "OrderCanceled",
    ]
    assert events[0].filled_qty == "33300"
    assert events[0].leaves_qty == "4162"
    assert events[1].status == "CANCELED"
    assert events[1].recovered is True


def test_manual_and_terminal_local_orders_are_excluded() -> None:
    reconciler = BinanceOwnedOrderReconciler(
        transport=RecordingTransport({})
    )

    captured = reconciler.capture(
        (
            SimpleNamespace(
                client_order_id="aos_manual_order",
                instrument_id="PENGUUSDT-PERP.BINANCE",
                status="ACCEPTED",
            ),
            SimpleNamespace(
                client_order_id=CLIENT_ORDER_ID,
                instrument_id="PENGUUSDT-PERP.BINANCE",
                status="FILLED",
            ),
        )
    )

    assert captured == ()


def test_trade_order_identity_mismatch_fails_closed() -> None:
    transport = RecordingTransport(
        {
            ("GET", "/fapi/v1/order"): {
                "symbol": "PENGUUSDT",
                "orderId": 88,
                "clientOrderId": CLIENT_ORDER_ID,
                "status": "FILLED",
                "side": "BUY",
                "origQty": "1",
                "executedQty": "1",
            },
            ("GET", "/fapi/v1/userTrades"): [
                {
                    "symbol": "PENGUUSDT",
                    "orderId": 89,
                    "id": 99,
                    "price": "0.009009",
                    "qty": "1",
                    "quoteQty": "0.009009",
                    "commission": "0.0000036",
                    "commissionAsset": "USDT",
                    "realizedPnl": "0",
                    "time": 1787644801000,
                }
            ],
        }
    )
    reconciler = BinanceOwnedOrderReconciler(transport=transport)
    order = SimpleNamespace(
        client_order_id=CLIENT_ORDER_ID,
        venue_order_id="88",
        instrument_id="PENGUUSDT-PERP.BINANCE",
        status="ACCEPTED",
    )

    with pytest.raises(
        OwnedOrderRecoveryError,
        match="userTrades orderId does not match order",
    ):
        reconciler.recover(reconciler.capture((order,)))


def test_projection_long_side_is_normalized_to_binance_buy() -> None:
    transport = RecordingTransport(
        {
            ("GET", "/fapi/v1/order"): {
                "symbol": "PENGUUSDT",
                "orderId": 91,
                "clientOrderId": CLIENT_ORDER_ID,
                "status": "NEW",
                "side": "BUY",
                "positionSide": "BOTH",
                "origQty": "33300",
                "executedQty": "0",
            },
            ("GET", "/fapi/v1/userTrades"): [],
        }
    )
    reconciler = BinanceOwnedOrderReconciler(transport=transport)
    projected_order = {
        "client_order_id": CLIENT_ORDER_ID,
        "venue_order_id": "91",
        "instrument_id": "PENGUUSDT-PERP.BINANCE",
        "status": "accepted",
        "side": "long",
        "position_side": "BOTH",
    }

    captured = reconciler.capture((projected_order,))

    assert captured[0].side == "BUY"
    assert reconciler.recover(captured) == ()


def test_missing_local_position_side_accepts_exchange_hedge_side() -> None:
    transport = RecordingTransport(
        {
            ("GET", "/fapi/v1/order"): {
                "symbol": "PENGUUSDT",
                "orderId": 93,
                "clientOrderId": CLIENT_ORDER_ID,
                "status": "NEW",
                "side": "BUY",
                "positionSide": "LONG",
                "origQty": "33300",
                "executedQty": "0",
            },
            ("GET", "/fapi/v1/userTrades"): [],
        }
    )
    reconciler = BinanceOwnedOrderReconciler(transport=transport)
    projected_order = {
        "client_order_id": CLIENT_ORDER_ID,
        "venue_order_id": "93",
        "instrument_id": "PENGUUSDT-PERP.BINANCE",
        "status": "accepted",
        "side": "long",
    }

    captured = reconciler.capture((projected_order,))

    assert captured[0].position_side == ""
    assert reconciler.recover(captured) == ()


def test_duplicate_venue_trade_id_fails_closed() -> None:
    duplicate_trade = {
        "symbol": "PENGUUSDT",
        "orderId": 92,
        "id": 99,
        "price": "0.009009",
        "qty": "1",
        "quoteQty": "0.009009",
        "commission": "0.0000036",
        "commissionAsset": "USDT",
        "realizedPnl": "0",
        "side": "BUY",
        "positionSide": "BOTH",
        "time": 1787644801000,
    }
    transport = RecordingTransport(
        {
            ("GET", "/fapi/v1/order"): {
                "symbol": "PENGUUSDT",
                "orderId": 92,
                "clientOrderId": CLIENT_ORDER_ID,
                "status": "FILLED",
                "side": "BUY",
                "positionSide": "BOTH",
                "origQty": "2",
                "executedQty": "2",
            },
            ("GET", "/fapi/v1/userTrades"): [
                duplicate_trade,
                dict(duplicate_trade),
            ],
        }
    )
    reconciler = BinanceOwnedOrderReconciler(transport=transport)
    order = SimpleNamespace(
        client_order_id=CLIENT_ORDER_ID,
        venue_order_id="92",
        instrument_id="PENGUUSDT-PERP.BINANCE",
        status="ACCEPTED",
        side="BUY",
        position_side="BOTH",
    )

    with pytest.raises(
        OwnedOrderRecoveryError,
        match="duplicate trade id",
    ):
        reconciler.recover(reconciler.capture((order,)))


class RecordingTransport:
    def __init__(
        self,
        responses: dict[tuple[str, str], Any],
    ) -> None:
        self._responses = dict(responses)
        self.calls: list[
            tuple[str, str, dict[str, Any], float | None]
        ] = []

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        self.calls.append(
            (
                method,
                path,
                dict(params),
                timeout_seconds,
            )
        )
        return self._responses[(method, path)]
