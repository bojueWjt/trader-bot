from __future__ import annotations

from commands.cancel_all import CancelAllSettings, cancel_all


class FakeVenue:
    def __init__(self, order_ids, *, fail_once=frozenset(), sticky_fail=frozenset()):
        self.working = {order_id: {"order_id": order_id, "instrument_id": "BTCUSDT"} for order_id in order_ids}
        self.fail_once = set(fail_once)
        self.sticky_fail = set(sticky_fail)
        self.attempts: dict[str, int] = {}
        self.verify_calls = 0

    def list_working_orders(self, **_kwargs):
        self.verify_calls += 1
        return list(self.working.values())

    def cancel_order(self, order):
        order_id = order["order_id"]
        self.attempts[order_id] = self.attempts.get(order_id, 0) + 1
        if order_id in self.sticky_fail:
            raise RuntimeError(f"{order_id} rate limited")
        if order_id in self.fail_once and self.attempts[order_id] == 1:
            raise RuntimeError(f"{order_id} transient")
        self.working.pop(order_id, None)
        return {"venue_order_id": order_id}


def test_cancel_all_retries_each_order_and_completes_only_after_no_working_orders():
    venue = FakeVenue(["o-1", "o-2", "o-3"], fail_once={"o-2"})
    sleeps: list[float] = []

    result = cancel_all(
        venue,
        account_id="acct-1",
        settings=CancelAllSettings(max_attempts=2, inter_order_delay_seconds=0.01),
        sleep=sleeps.append,
    )

    assert result["status"] == "completed"
    assert result["verification"]["working_order_count"] == 0
    assert [item["order_id"] for item in result["orders"]] == ["o-1", "o-2", "o-3"]
    assert {item["order_id"]: item["status"] for item in result["orders"]} == {
        "o-1": "cancel_requested",
        "o-2": "cancel_requested",
        "o-3": "cancel_requested",
    }
    assert venue.attempts["o-2"] == 2
    assert venue.verify_calls >= 2
    assert sleeps


def test_cancel_all_returns_partial_when_final_verification_finds_residual_orders():
    venue = FakeVenue(["o-1", "o-2"], sticky_fail={"o-2"})

    result = cancel_all(
        venue,
        account_id="acct-1",
        settings=CancelAllSettings(max_attempts=2, verify_attempts=1),
        sleep=lambda _seconds: None,
    )

    assert result["status"] == "partial"
    assert result["verification"]["working_order_count"] == 1
    assert result["orders"][1]["status"] == "failed"
    assert result["remaining_working_orders"][0]["order_id"] == "o-2"
