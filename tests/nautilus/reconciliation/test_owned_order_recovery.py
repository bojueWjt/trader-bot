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
from runtime.exchange_cancel_adapter import BinanceApiError  # noqa: E402


CLIENT_ORDER_ID = "B3562ddc2a0e74f509dc34253ab80beef01"
SECOND_CLIENT_ORDER_ID = "B3562ddc2a0e74f509dc34253ab80beef02"


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


def test_missing_venue_order_without_order_id_recovers_canceled_event() -> None:
    transport = RecordingTransport(
        {
            ("GET", "/fapi/v1/order"): BinanceApiError(
                -2013,
                "Order does not exist.",
            ),
        }
    )
    reconciler = BinanceOwnedOrderReconciler(transport=transport)
    order = SimpleNamespace(
        client_order_id=CLIENT_ORDER_ID,
        venue_order_id="",
        instrument_id="PENGUUSDT-PERP.BINANCE",
        status="ACCEPTED",
        side="BUY",
        position_side="BOTH",
        order_type="LIMIT",
        time_in_force="GTC",
        tags=("intent_id=3562ddc2-a0e7-4f50-9dc3-4253ab80beef",),
    )

    events = reconciler.recover(reconciler.capture((order,)))

    assert len(events) == 1
    canceled = events[0]
    assert canceled.event_type == "OrderCanceled"
    assert canceled.status == "CANCELED"
    assert canceled.venue_order_id == ""
    assert "venue_order_missing(-2013)" in canceled.reason
    assert "fill_attribution_unverifiable" in canceled.reason
    assert canceled.recovered is True
    assert canceled.source == "exchange_reconciliation"
    assert "order_vanished" in canceled.tags
    assert [call[1] for call in transport.calls] == [
        "/fapi/v1/order",
    ]


def test_missing_venue_order_with_trades_recovers_fills_then_canceled() -> None:
    transport = RecordingTransport(
        {
            ("GET", "/fapi/v1/order"): BinanceApiError(
                -2013,
                "Order does not exist.",
            ),
            ("GET", "/fapi/v1/userTrades"): [
                {
                    "symbol": "SNDKUSDT",
                    "orderId": 82901,
                    "id": 501,
                    "price": "198.10",
                    "qty": "0.40",
                    "quoteQty": "79.24",
                    "commission": "0.031696",
                    "commissionAsset": "USDT",
                    "realizedPnl": "0",
                    "side": "SELL",
                    "positionSide": "BOTH",
                    "buyer": False,
                    "maker": True,
                    "time": 1787990401000,
                },
                {
                    "symbol": "SNDKUSDT",
                    "orderId": 82901,
                    "id": 502,
                    "price": "198.00",
                    "qty": "0.59",
                    "quoteQty": "116.82",
                    "commission": "0.046728",
                    "commissionAsset": "USDT",
                    "realizedPnl": "0",
                    "side": "SELL",
                    "positionSide": "BOTH",
                    "buyer": False,
                    "maker": True,
                    "time": 1787990402000,
                },
            ],
        }
    )
    reconciler = BinanceOwnedOrderReconciler(transport=transport)
    order = SimpleNamespace(
        client_order_id=CLIENT_ORDER_ID,
        venue_order_id="82901",
        instrument_id="SNDKUSDT-PERP.BINANCE",
        status="ACCEPTED",
        side="SELL",
        position_side="BOTH",
        order_type="LIMIT",
        time_in_force="GTC",
        reduce_only=False,
    )

    events = reconciler.recover(reconciler.capture((order,)))

    assert [event.event_type for event in events] == [
        "OrderFilled",
        "OrderFilled",
        "OrderCanceled",
    ]
    fills = events[:2]
    assert [fill.last_qty for fill in fills] == ["0.40", "0.59"]
    assert [fill.filled_qty for fill in fills] == ["0.40", "0.99"]
    assert all(fill.quantity == "" for fill in fills)
    assert all(fill.leaves_qty == "" for fill in fills)
    assert all("order_vanished" in fill.tags for fill in fills)
    canceled = events[2]
    assert canceled.status == "CANCELED"
    assert canceled.filled_qty == "0.99"
    assert "venue_order_missing(-2013)" in canceled.reason
    assert "attributed_trade_count=2" in canceled.reason
    assert "attributed_filled_qty=0.99" in canceled.reason
    assert "order_vanished" in canceled.tags
    assert [call[1] for call in transport.calls] == [
        "/fapi/v1/order",
        "/fapi/v1/userTrades",
    ]
    assert transport.calls[1][2] == {
        "symbol": "SNDKUSDT",
        "orderId": "82901",
        "limit": 1000,
    }


def test_missing_venue_order_without_trades_recovers_canceled_event() -> None:
    transport = RecordingTransport(
        {
            ("GET", "/fapi/v1/order"): BinanceApiError(
                -2013,
                "Order does not exist.",
            ),
            ("GET", "/fapi/v1/userTrades"): [],
        }
    )
    reconciler = BinanceOwnedOrderReconciler(transport=transport)
    order = SimpleNamespace(
        client_order_id=CLIENT_ORDER_ID,
        venue_order_id="82902",
        instrument_id="SNDKUSDT-PERP.BINANCE",
        status="ACCEPTED",
        side="SELL",
    )

    events = reconciler.recover(reconciler.capture((order,)))

    assert len(events) == 1
    canceled = events[0]
    assert canceled.event_type == "OrderCanceled"
    assert canceled.status == "CANCELED"
    assert canceled.venue_order_id == "82902"
    assert canceled.filled_qty == ""
    assert "venue_order_missing(-2013)" in canceled.reason
    assert "attributed_trade_count=0" in canceled.reason
    assert "order_vanished" in canceled.tags
    assert [call[1] for call in transport.calls] == [
        "/fapi/v1/order",
        "/fapi/v1/userTrades",
    ]


def test_non_missing_binance_order_error_is_raised_unchanged() -> None:
    error = BinanceApiError(-1021, "Timestamp outside recvWindow.")
    transport = RecordingTransport(
        {
            ("GET", "/fapi/v1/order"): error,
        }
    )
    reconciler = BinanceOwnedOrderReconciler(transport=transport)
    order = SimpleNamespace(
        client_order_id=CLIENT_ORDER_ID,
        venue_order_id="82903",
        instrument_id="SNDKUSDT-PERP.BINANCE",
        status="ACCEPTED",
    )

    with pytest.raises(BinanceApiError) as raised:
        reconciler.recover(reconciler.capture((order,)))

    assert raised.value is error


def test_missing_venue_order_does_not_block_later_candidate_recovery() -> None:
    missing_key = (
        "GET",
        "/fapi/v1/order",
        (
            ("orderId", "82904"),
            ("symbol", "SNDKUSDT"),
        ),
    )
    recovered_key = (
        "GET",
        "/fapi/v1/order",
        (
            ("orderId", "82905"),
            ("symbol", "PENGUUSDT"),
        ),
    )
    transport = RecordingTransport(
        {
            missing_key: BinanceApiError(
                -2013,
                "Order does not exist.",
            ),
            recovered_key: {
                "symbol": "PENGUUSDT",
                "orderId": 82905,
                "clientOrderId": SECOND_CLIENT_ORDER_ID,
                "status": "CANCELED",
                "side": "BUY",
                "positionSide": "BOTH",
                "type": "LIMIT",
                "timeInForce": "GTC",
                "origQty": "10",
                "executedQty": "0",
                "price": "0.009",
                "avgPrice": "0",
                "reduceOnly": False,
                "updateTime": 1787990403000,
            },
            ("GET", "/fapi/v1/userTrades"): [],
        }
    )
    reconciler = BinanceOwnedOrderReconciler(transport=transport)
    missing = SimpleNamespace(
        client_order_id=CLIENT_ORDER_ID,
        venue_order_id="82904",
        instrument_id="SNDKUSDT-PERP.BINANCE",
        status="ACCEPTED",
        side="SELL",
    )
    recoverable = SimpleNamespace(
        client_order_id=SECOND_CLIENT_ORDER_ID,
        venue_order_id="82905",
        instrument_id="PENGUUSDT-PERP.BINANCE",
        status="ACCEPTED",
        side="BUY",
        position_side="BOTH",
    )

    events = reconciler.recover(
        reconciler.capture((missing, recoverable))
    )

    assert [event.client_order_id for event in events] == [
        CLIENT_ORDER_ID,
        SECOND_CLIENT_ORDER_ID,
    ]
    assert [event.event_type for event in events] == [
        "OrderCanceled",
        "OrderCanceled",
    ]
    assert [call[2]["orderId"] for call in transport.calls] == [
        "82904",
        "82904",
        "82905",
        "82905",
    ]


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
        responses: dict[tuple[Any, ...], Any],
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
        request_key = (
            method,
            path,
            tuple(sorted(params.items())),
        )
        response = self._responses.get(request_key)
        if request_key not in self._responses:
            response = self._responses[(method, path)]
        if isinstance(response, Exception):
            raise response
        return response
