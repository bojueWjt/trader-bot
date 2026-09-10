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
            ("GET", "/fapi/v1/algoOrder"): BinanceApiError(
                -2013,
                "Algo order does not exist.",
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
        "/fapi/v1/algoOrder",
    ]


def test_missing_venue_order_with_trades_recovers_fills_then_canceled() -> None:
    transport = RecordingTransport(
        {
            ("GET", "/fapi/v1/order"): BinanceApiError(
                -2013,
                "Order does not exist.",
            ),
            ("GET", "/fapi/v1/algoOrder"): BinanceApiError(
                -2013,
                "Algo order does not exist.",
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
        "/fapi/v1/algoOrder",
        "/fapi/v1/userTrades",
    ]
    assert transport.calls[2][2] == {
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
            ("GET", "/fapi/v1/algoOrder"): BinanceApiError(
                -2013,
                "Algo order does not exist.",
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
        "/fapi/v1/algoOrder",
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


def test_algo_order_new_is_left_untouched_while_regular_order_recovers() -> None:
    algo_venue_order_id = "2000001419214132"
    algo_order_key = (
        "GET",
        "/fapi/v1/order",
        (
            ("orderId", algo_venue_order_id),
            ("symbol", "ATOMUSDT"),
        ),
    )
    regular_order_key = (
        "GET",
        "/fapi/v1/order",
        (
            ("orderId", "82910"),
            ("symbol", "PENGUUSDT"),
        ),
    )
    transport = RecordingTransport(
        {
            algo_order_key: BinanceApiError(
                -2013,
                "Order does not exist.",
            ),
            (
                "GET",
                "/fapi/v1/algoOrder",
                (
                    ("algoId", algo_venue_order_id),
                    ("symbol", "ATOMUSDT"),
                ),
            ): {
                "symbol": "ATOMUSDT",
                "algoId": int(algo_venue_order_id),
                "clientAlgoId": CLIENT_ORDER_ID,
                "algoStatus": "NEW",
            },
            regular_order_key: {
                "symbol": "PENGUUSDT",
                "orderId": 82910,
                "clientOrderId": SECOND_CLIENT_ORDER_ID,
                "status": "CANCELED",
                "side": "BUY",
                "positionSide": "BOTH",
                "type": "LIMIT",
                "timeInForce": "GTC",
                "origQty": "1",
                "executedQty": "0",
                "price": "1",
                "avgPrice": "0",
                "reduceOnly": False,
                "updateTime": 1787990404000,
            },
            ("GET", "/fapi/v1/userTrades"): [],
        }
    )
    reconciler = BinanceOwnedOrderReconciler(transport=transport)
    algo_order = SimpleNamespace(
        client_order_id=CLIENT_ORDER_ID,
        venue_order_id=algo_venue_order_id,
        instrument_id="ATOMUSDT-PERP.BINANCE",
        status="ACCEPTED",
        side="SELL",
    )
    regular_order = SimpleNamespace(
        client_order_id=SECOND_CLIENT_ORDER_ID,
        venue_order_id="82910",
        instrument_id="PENGUUSDT-PERP.BINANCE",
        status="ACCEPTED",
        side="BUY",
        position_side="BOTH",
    )

    events = reconciler.recover(
        reconciler.capture((algo_order, regular_order))
    )

    assert [event.client_order_id for event in events] == [
        SECOND_CLIENT_ORDER_ID,
    ]
    assert events[0].event_type == "OrderCanceled"
    assert [call[1] for call in transport.calls] == [
        "/fapi/v1/order",
        "/fapi/v1/algoOrder",
        "/fapi/v1/order",
        "/fapi/v1/userTrades",
    ]


def test_algo_order_missing_falls_back_to_missing_recovery() -> None:
    venue_order_id = "2000001419214133"
    transport = RecordingTransport(
        {
            ("GET", "/fapi/v1/order"): BinanceApiError(
                -2013,
                "Order does not exist.",
            ),
            ("GET", "/fapi/v1/algoOrder"): BinanceApiError(
                -2013,
                "Algo order does not exist.",
            ),
            ("GET", "/fapi/v1/userTrades"): [],
        }
    )
    reconciler = BinanceOwnedOrderReconciler(transport=transport)
    order = SimpleNamespace(
        client_order_id=CLIENT_ORDER_ID,
        venue_order_id=venue_order_id,
        instrument_id="ATOMUSDT-PERP.BINANCE",
        status="ACCEPTED",
        side="SELL",
    )

    events = reconciler.recover(reconciler.capture((order,)))

    assert len(events) == 1
    assert events[0].event_type == "OrderCanceled"
    assert "venue_order_missing(-2013)" in events[0].reason


def test_algo_order_canceled_falls_back_to_missing_recovery() -> None:
    venue_order_id = "2000001419214134"
    transport = RecordingTransport(
        {
            ("GET", "/fapi/v1/order"): BinanceApiError(
                -2013,
                "Order does not exist.",
            ),
            ("GET", "/fapi/v1/algoOrder"): {
                "symbol": "ATOMUSDT",
                "algoId": venue_order_id,
                "clientAlgoId": CLIENT_ORDER_ID,
                "algoStatus": "CANCELED",
            },
        }
    )
    reconciler = BinanceOwnedOrderReconciler(transport=transport)
    order = SimpleNamespace(
        client_order_id=CLIENT_ORDER_ID,
        venue_order_id=venue_order_id,
        instrument_id="ATOMUSDT-PERP.BINANCE",
        status="ACCEPTED",
        side="SELL",
    )

    events = reconciler.recover(reconciler.capture((order,)))

    assert len(events) == 1
    assert events[0].event_type == "OrderCanceled"
    assert events[0].venue_order_id == venue_order_id
    assert events[0].reason == "algo_order_terminal(CANCELED)"
    assert [call[1] for call in transport.calls] == [
        "/fapi/v1/order",
        "/fapi/v1/algoOrder",
    ]


def test_algo_order_identity_mismatch_fails_closed() -> None:
    venue_order_id = "2000001419214135"
    transport = RecordingTransport(
        {
            ("GET", "/fapi/v1/order"): BinanceApiError(
                -2013,
                "Order does not exist.",
            ),
            ("GET", "/fapi/v1/algoOrder"): {
                "symbol": "ATOMUSDT",
                "algoId": "2000001419214999",
                "clientAlgoId": CLIENT_ORDER_ID,
                "algoStatus": "NEW",
            },
        }
    )
    reconciler = BinanceOwnedOrderReconciler(transport=transport)
    order = SimpleNamespace(
        client_order_id=CLIENT_ORDER_ID,
        venue_order_id=venue_order_id,
        instrument_id="ATOMUSDT-PERP.BINANCE",
        status="ACCEPTED",
        side="SELL",
    )

    with pytest.raises(
        OwnedOrderRecoveryError,
        match="algoId does not match candidate",
    ):
        reconciler.recover(reconciler.capture((order,)))


def test_algo_order_unknown_status_is_left_untouched() -> None:
    transport = RecordingTransport(
        {
            ("GET", "/fapi/v1/order"): BinanceApiError(
                -2013,
                "Order does not exist.",
            ),
            ("GET", "/fapi/v1/algoOrder"): {
                "symbol": "ATOMUSDT",
                "algoId": 2000001419214136,
                "clientAlgoId": CLIENT_ORDER_ID,
                "algoStatus": "WHATEVER",
            },
        }
    )
    reconciler = BinanceOwnedOrderReconciler(transport=transport)
    order = SimpleNamespace(
        client_order_id=CLIENT_ORDER_ID,
        venue_order_id="",
        instrument_id="ATOMUSDT-PERP.BINANCE",
        status="ACCEPTED",
        side="SELL",
    )

    events = reconciler.recover(reconciler.capture((order,)))

    assert events == ()
    assert transport.calls[1][2] == {
        "symbol": "ATOMUSDT",
        "clientAlgoId": CLIENT_ORDER_ID,
    }


def test_algo_order_non_missing_binance_error_is_raised_unchanged() -> None:
    error = BinanceApiError(-1021, "Timestamp outside recvWindow.")
    transport = RecordingTransport(
        {
            ("GET", "/fapi/v1/order"): BinanceApiError(
                -2013,
                "Order does not exist.",
            ),
            ("GET", "/fapi/v1/algoOrder"): error,
        }
    )
    reconciler = BinanceOwnedOrderReconciler(transport=transport)
    order = SimpleNamespace(
        client_order_id=CLIENT_ORDER_ID,
        venue_order_id="2000001419214137",
        instrument_id="ATOMUSDT-PERP.BINANCE",
        status="ACCEPTED",
        side="SELL",
    )

    with pytest.raises(BinanceApiError) as raised:
        reconciler.recover(reconciler.capture((order,)))

    assert raised.value is error


@pytest.mark.parametrize("algo_status", ["TRIGGERED", "FINISHED"])
def test_executed_algo_with_actual_order_recovers_attributed_fill(
    algo_status: str,
) -> None:
    algo_order_id = "2000001419214200"
    actual_order_id = "82920"
    transport = RecordingTransport(
        {
            (
                "GET",
                "/fapi/v1/order",
                (
                    ("orderId", algo_order_id),
                    ("symbol", "ATOMUSDT"),
                ),
            ): BinanceApiError(-2013, "Order does not exist."),
            ("GET", "/fapi/v1/algoOrder"): {
                "symbol": "ATOMUSDT",
                "algoId": algo_order_id,
                "clientAlgoId": CLIENT_ORDER_ID,
                "algoStatus": algo_status,
                "actualOrderId": actual_order_id,
            },
            (
                "GET",
                "/fapi/v1/order",
                (
                    ("orderId", actual_order_id),
                    ("symbol", "ATOMUSDT"),
                ),
            ): {
                "symbol": "ATOMUSDT",
                "orderId": actual_order_id,
                "clientOrderId": "spawned-from-algo-order",
                "status": "FILLED",
                "side": "SELL",
                "positionSide": "BOTH",
                "type": "MARKET",
                "timeInForce": "GTC",
                "origQty": "2",
                "executedQty": "2",
                "avgPrice": "10",
                "price": "0",
                "updateTime": 1787990405000,
            },
            ("GET", "/fapi/v1/userTrades"): [
                {
                    "symbol": "ATOMUSDT",
                    "orderId": actual_order_id,
                    "id": "501",
                    "price": "10",
                    "qty": "2",
                    "quoteQty": "20",
                    "commission": "0.01",
                    "commissionAsset": "USDT",
                    "realizedPnl": "0",
                    "side": "SELL",
                    "positionSide": "BOTH",
                    "time": 1787990404000,
                }
            ],
        }
    )
    reconciler = BinanceOwnedOrderReconciler(transport=transport)
    order = SimpleNamespace(
        client_order_id=CLIENT_ORDER_ID,
        venue_order_id=algo_order_id,
        instrument_id="ATOMUSDT-PERP.BINANCE",
        status="ACCEPTED",
        side="SELL",
        position_side="BOTH",
    )

    events = reconciler.recover(reconciler.capture((order,)))

    assert len(events) == 1
    assert events[0].event_type == "OrderFilled"
    assert events[0].client_order_id == CLIENT_ORDER_ID
    assert events[0].venue_order_id == actual_order_id
    assert events[0].filled_qty == "2"
    assert transport.calls[2][2] == {
        "symbol": "ATOMUSDT",
        "orderId": actual_order_id,
    }
    assert transport.calls[3][2]["orderId"] == actual_order_id


@pytest.mark.parametrize("algo_status", ["TRIGGERED", "FINISHED"])
def test_executed_algo_without_actual_order_id_produces_no_events(
    algo_status: str,
) -> None:
    transport = RecordingTransport(
        {
            ("GET", "/fapi/v1/order"): BinanceApiError(
                -2013,
                "Order does not exist.",
            ),
            ("GET", "/fapi/v1/algoOrder"): {
                "symbol": "ATOMUSDT",
                "algoId": "2000001419214201",
                "clientAlgoId": CLIENT_ORDER_ID,
                "algoStatus": algo_status,
            },
        }
    )
    reconciler = BinanceOwnedOrderReconciler(transport=transport)
    order = SimpleNamespace(
        client_order_id=CLIENT_ORDER_ID,
        venue_order_id="2000001419214201",
        instrument_id="ATOMUSDT-PERP.BINANCE",
        status="ACCEPTED",
    )

    events = reconciler.recover(reconciler.capture((order,)))

    assert events == ()
    assert [call[1] for call in transport.calls] == [
        "/fapi/v1/order",
        "/fapi/v1/algoOrder",
    ]


@pytest.mark.parametrize(
    ("algo_payload", "error_match"),
    [
        (
            {
                "algoId": "2000001419214202",
                "clientAlgoId": CLIENT_ORDER_ID,
                "algoStatus": "NEW",
            },
            "symbol",
        ),
        (
            {
                "symbol": "BTCUSDT",
                "algoId": "2000001419214202",
                "clientAlgoId": CLIENT_ORDER_ID,
                "algoStatus": "NEW",
            },
            "symbol does not match candidate",
        ),
        (
            {
                "symbol": "ATOMUSDT",
                "algoId": "2000001419214999",
                "clientAlgoId": CLIENT_ORDER_ID,
                "algoStatus": "NEW",
            },
            "algoId does not match candidate",
        ),
        (
            {
                "symbol": "ATOMUSDT",
                "clientAlgoId": CLIENT_ORDER_ID,
                "algoStatus": "NEW",
            },
            "missing algoId",
        ),
    ],
    ids=[
        "missing-symbol",
        "wrong-symbol",
        "only-client-id-matches",
        "missing-algo-id",
    ],
)
def test_algo_order_identity_failures_are_rejected(
    algo_payload: dict[str, Any],
    error_match: str,
) -> None:
    transport = RecordingTransport(
        {
            ("GET", "/fapi/v1/order"): BinanceApiError(
                -2013,
                "Order does not exist.",
            ),
            ("GET", "/fapi/v1/algoOrder"): algo_payload,
        }
    )
    reconciler = BinanceOwnedOrderReconciler(transport=transport)
    order = SimpleNamespace(
        client_order_id=CLIENT_ORDER_ID,
        venue_order_id="2000001419214202",
        instrument_id="ATOMUSDT-PERP.BINANCE",
        status="ACCEPTED",
    )

    with pytest.raises(OwnedOrderRecoveryError, match=error_match):
        reconciler.recover(reconciler.capture((order,)))


@pytest.mark.parametrize("error_code", [-1102, -2011, -4120])
def test_algo_order_non_missing_errors_are_raised_unchanged(
    error_code: int,
) -> None:
    error = BinanceApiError(error_code, "Algo query rejected.")
    transport = RecordingTransport(
        {
            ("GET", "/fapi/v1/order"): BinanceApiError(
                -2013,
                "Order does not exist.",
            ),
            ("GET", "/fapi/v1/algoOrder"): error,
        }
    )
    reconciler = BinanceOwnedOrderReconciler(transport=transport)
    order = SimpleNamespace(
        client_order_id=CLIENT_ORDER_ID,
        venue_order_id="2000001419214203",
        instrument_id="ATOMUSDT-PERP.BINANCE",
        status="ACCEPTED",
    )

    with pytest.raises(BinanceApiError) as raised:
        reconciler.recover(reconciler.capture((order,)))

    assert raised.value is error


def test_actual_order_recovery_propagates_deadline_to_every_request() -> None:
    algo_order_id = "2000001419214204"
    actual_order_id = "82924"
    transport = RecordingTransport(
        {
            (
                "GET",
                "/fapi/v1/order",
                (("orderId", algo_order_id), ("symbol", "ATOMUSDT")),
            ): BinanceApiError(-2013, "Order does not exist."),
            ("GET", "/fapi/v1/algoOrder"): {
                "symbol": "ATOMUSDT",
                "algoId": algo_order_id,
                "clientAlgoId": CLIENT_ORDER_ID,
                "algoStatus": "TRIGGERED",
                "actualOrderId": actual_order_id,
            },
            (
                "GET",
                "/fapi/v1/order",
                (("orderId", actual_order_id), ("symbol", "ATOMUSDT")),
            ): {
                "symbol": "ATOMUSDT",
                "orderId": actual_order_id,
                "clientOrderId": "spawned-from-algo-order",
                "status": "NEW",
                "side": "SELL",
                "origQty": "1",
                "executedQty": "0",
            },
            ("GET", "/fapi/v1/userTrades"): [],
        }
    )
    timestamps = iter((1.0, 2.0, 3.0, 4.0))
    reconciler = BinanceOwnedOrderReconciler(
        transport=transport,
        monotonic=lambda: next(timestamps),
    )
    order = SimpleNamespace(
        client_order_id=CLIENT_ORDER_ID,
        venue_order_id=algo_order_id,
        instrument_id="ATOMUSDT-PERP.BINANCE",
        status="ACCEPTED",
        side="SELL",
    )

    assert reconciler.recover(
        reconciler.capture((order,)),
        deadline_monotonic=10.0,
    ) == ()
    assert [call[3] for call in transport.calls] == [9.0, 8.0, 7.0, 6.0]


def test_actual_order_recovery_fails_when_deadline_expires_midway() -> None:
    algo_order_id = "2000001419214205"
    transport = RecordingTransport(
        {
            ("GET", "/fapi/v1/order"): BinanceApiError(
                -2013,
                "Order does not exist.",
            ),
            ("GET", "/fapi/v1/algoOrder"): {
                "symbol": "ATOMUSDT",
                "algoId": algo_order_id,
                "clientAlgoId": CLIENT_ORDER_ID,
                "algoStatus": "TRIGGERED",
                "actualOrderId": "82925",
            },
        }
    )
    timestamps = iter((1.0, 2.0, 10.0))
    reconciler = BinanceOwnedOrderReconciler(
        transport=transport,
        monotonic=lambda: next(timestamps),
    )
    order = SimpleNamespace(
        client_order_id=CLIENT_ORDER_ID,
        venue_order_id=algo_order_id,
        instrument_id="ATOMUSDT-PERP.BINANCE",
        status="ACCEPTED",
    )

    with pytest.raises(TimeoutError, match="deadline exceeded"):
        reconciler.recover(
            reconciler.capture((order,)),
            deadline_monotonic=10.0,
        )

    assert [call[1] for call in transport.calls] == [
        "/fapi/v1/order",
        "/fapi/v1/algoOrder",
    ]


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
            ("GET", "/fapi/v1/algoOrder"): BinanceApiError(
                -2013,
                "Algo order does not exist.",
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
    assert [call[1] for call in transport.calls] == [
        "/fapi/v1/order",
        "/fapi/v1/algoOrder",
        "/fapi/v1/userTrades",
        "/fapi/v1/order",
        "/fapi/v1/userTrades",
    ]
    assert transport.calls[1][2] == {
        "symbol": "SNDKUSDT",
        "algoId": "82904",
    }


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
