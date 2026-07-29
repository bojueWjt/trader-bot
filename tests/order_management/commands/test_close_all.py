from __future__ import annotations

from commands.close_all import CloseAllSettings, close_all


class FakeVenue:
    def __init__(self, positions):
        self.positions = {position["position_id"]: dict(position) for position in positions}

    def list_open_positions(self, **_kwargs):
        return [p for p in self.positions.values() if str(p.get("quantity")) != "0"]

    def mark_flat(self, position_key: str) -> None:
        for position in self.positions.values():
            if position["position_key"] == position_key:
                position["quantity"] = "0"


def test_close_all_closes_one_position_at_a_time_with_retry_and_final_flat_verify():
    venue = FakeVenue(
        [
            {
                "position_id": "pos-1",
                "position_key": "acct-1:BTCUSDT",
                "account_id": "acct-1",
                "instrument_id": "BTCUSDT-PERP.BINANCE",
                "position_side": "BOTH",
                "quantity": "0.1",
            },
            {
                "position_id": "pos-2",
                "position_key": "acct-1:ETHUSDT",
                "account_id": "acct-1",
                "instrument_id": "ETHUSDT-PERP.BINANCE",
                "position_side": "BOTH",
                "quantity": "1.0",
            },
        ]
    )
    attempts: dict[str, int] = {}
    call_order: list[str] = []
    sleeps: list[float] = []

    def close_one(**kwargs):
        key = kwargs["position_key"]
        attempts[key] = attempts.get(key, 0) + 1
        call_order.append(key)
        if key == "acct-1:ETHUSDT" and attempts[key] == 1:
            raise RuntimeError("transient close failure")
        venue.mark_flat(key)
        return {"position_key": key, "reduce_only": kwargs["reduce_only"]}

    result = close_all(
        None,
        account_id="acct-1",
        venue=venue,
        close_one=close_one,
        settings=CloseAllSettings(max_attempts=2, inter_position_delay_seconds=0.01),
        sleep=sleeps.append,
    )

    assert result["status"] == "completed"
    assert result["verification"]["flat"] is True
    assert call_order == ["acct-1:BTCUSDT", "acct-1:ETHUSDT", "acct-1:ETHUSDT"]
    assert {item["position_key"]: item["status"] for item in result["positions"]} == {
        "acct-1:BTCUSDT": "closed",
        "acct-1:ETHUSDT": "closed",
    }
    assert all(item["reduce_only"] for item in result["positions"])
    assert sleeps


def test_close_all_is_partial_until_all_target_positions_are_venue_flat():
    venue = FakeVenue(
        [
            {
                "position_id": "pos-1",
                "position_key": "acct-1:BTCUSDT",
                "account_id": "acct-1",
                "instrument_id": "BTCUSDT-PERP.BINANCE",
                "position_side": "BOTH",
                "quantity": "0.1",
            }
        ]
    )

    def close_one(**kwargs):
        return {"position_key": kwargs["position_key"], "accepted": True}

    result = close_all(
        None,
        account_id="acct-1",
        venue=venue,
        close_one=close_one,
        settings=CloseAllSettings(max_attempts=1, verify_attempts=1),
        sleep=lambda _seconds: None,
    )

    assert result["status"] == "partial"
    assert result["verification"]["flat"] is False
    assert result["residual_positions"][0]["position_key"] == "acct-1:BTCUSDT"
