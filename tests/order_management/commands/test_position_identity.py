from __future__ import annotations

from commands.close_all import CloseAllSettings, close_all
from commands.position_identity import (
    close_boundary_flat,
    merge_position_snapshots,
    position_key_for_snapshot,
)


def test_external_both_and_local_both_map_to_same_venue_position_key():
    external = {
        "account_id": "acct-1",
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "position_side": "BOTH",
        "ownership": "EXTERNAL",
        "quantity": "0.2",
    }
    local = {
        "account_id": "acct-1",
        "instrument_id": "BTCUSDT",
        "position_mode": "ONEWAY",
        "position_side": "BOTH",
        "source": "BOT",
        "quantity": "0.2",
    }

    assert position_key_for_snapshot("acct-1", external) == "acct-1:BTCUSDT"
    assert position_key_for_snapshot("acct-1", local) == "acct-1:BTCUSDT"


def test_merge_position_snapshots_collapses_external_and_local_phantoms():
    merged = merge_position_snapshots(
        "acct-1",
        [
            {"position_id": "venue", "instrument_id": "BTCUSDT", "position_side": "BOTH", "ownership": "EXTERNAL", "quantity": "0.2"},
            {"position_id": "phantom", "instrument_id": "BTCUSDT-PERP.BINANCE", "position_side": "BOTH", "source": "BOT", "quantity": "0.2"},
            {"position_id": "zero", "instrument_id": "ETHUSDT", "position_side": "BOTH", "quantity": "0"},
        ],
    )

    assert len(merged) == 1
    assert merged[0]["position_key"] == "acct-1:BTCUSDT"
    assert merged[0]["quantity"] == "0.2"
    assert set(merged[0]["source_position_ids"]) == {"venue", "phantom"}


def test_close_boundary_requires_db_and_venue_flat_for_targets():
    target = {"acct-1:BTCUSDT"}
    assert close_boundary_flat("acct-1", target, venue_positions=[], db_positions=[]) is True
    assert close_boundary_flat(
        "acct-1",
        target,
        venue_positions=[],
        db_positions=[{"instrument_id": "BTCUSDT", "position_side": "BOTH", "quantity": "0.1"}],
    ) is False


def test_close_all_completes_external_both_position_when_venue_verifies_flat():
    class Venue:
        def __init__(self):
            self.position = {
                "position_id": "external",
                "account_id": "acct-1",
                "instrument_id": "BTCUSDT-PERP.BINANCE",
                "position_side": "BOTH",
                "ownership": "EXTERNAL",
                "quantity": "0.2",
            }

        def list_open_positions(self, **_kwargs):
            if self.position["quantity"] == "0":
                return []
            return [self.position]

    venue = Venue()

    def close_one(**kwargs):
        assert kwargs["position_key"] == "acct-1:BTCUSDT"
        venue.position["quantity"] = "0"
        return {"closed": kwargs["position_key"]}

    result = close_all(
        None,
        account_id="acct-1",
        venue=venue,
        close_one=close_one,
        settings=CloseAllSettings(max_attempts=1, verify_attempts=1),
        sleep=lambda _seconds: None,
    )

    assert result["status"] == "completed"
    assert result["verification"]["flat"] is True
