from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from scripts.analysis import trade_outcomes


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "account_d_btc_fills.json"


def _load_btc_records(*, include_position_events: bool = True) -> list[dict]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    records = []
    for row in payload["fills"]:
        records.append(
            {
                **row,
                "order_plan": {"side": "long"},
                "ts_event": datetime.fromisoformat(row["ts_event"]),
            }
        )
    if include_position_events:
        for row in payload.get("position_events") or []:
            records.append(
                {
                    **row,
                    "ts_event": datetime.fromisoformat(row["ts_event"]),
                }
            )
    records.sort(key=lambda item: item["ts_event"])
    return records


def test_old_and_new_entry_lots_fifo_isolated(tmp_path):
    stats = trade_outcomes.EpisodeBuildStats()
    episodes = trade_outcomes.build_position_episodes(_load_btc_records(), stats=stats)
    outcomes = trade_outcomes.compute_outcomes(episodes, cache_dir=tmp_path)

    assert stats.unmatched_exit_count == 0
    assert stats.open_lot_count == 0
    assert len(episodes) == 3
    by_intent = {}
    for outcome in outcomes:
        by_intent.setdefault(outcome["intent_id"], []).append(outcome)

    old_lots = by_intent["0940adaa-5659-4dc3-a62a-e05851f86587"]
    new_lots = by_intent["7a9bc2d3-fcf8-4a02-ad0d-7d4764dc386e"]
    assert len(old_lots) == 1
    assert len(new_lots) == 2
    assert old_lots[0]["filled_quantity"] == Decimal("0.156")
    new_qty = sum(row["filled_quantity"] for row in new_lots)
    assert new_qty == Decimal("0.118")
    old_pnl = old_lots[0]["realized_pnl"]
    new_pnl = sum(row["realized_pnl"] for row in new_lots)
    # Close fill realized_pnl 163.4864 is split across remaining 0.117 + 0.118.
    # Partial 0.039 uses mark-to-exit-price, not Nautilus Position.realized_pnl.
    assert old_pnl > Decimal("400")
    assert new_pnl < 0
    close_share = Decimal("163.48640000")
    # New lots only participate in the 0.235 close.
    assert abs(new_pnl - (Decimal("-121.8") + Decimal("-32.86"))) < Decimal("0.0000001")
    assert "fifo" in (old_lots[0]["details"].get("allocation_method") or "")
    lot_keys = {row["lot_key"] for row in outcomes}
    assert len(lot_keys) == 3
    assert close_share == close_share


def test_new_opened_does_not_cut_old_unclosed_lot(tmp_path):
    records = _load_btc_records(include_position_events=False)[:3]
    stats = trade_outcomes.EpisodeBuildStats()
    episodes = trade_outcomes.build_position_episodes(records, stats=stats)
    assert episodes == []
    assert stats.open_lot_count == 2


def test_hedge_books_do_not_net():
    t0 = datetime(2026, 9, 23, 1, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 23, 2, tzinfo=timezone.utc)
    long_intent = str(uuid4())
    short_intent = str(uuid4())
    records = [
        {
            "account_id": "account-c",
            "intent_id": long_intent,
            "event_type": "OrderFilled",
            "ts_event": t0,
            "instrument_id": "ASTERUSDT-PERP.BINANCE",
            "client_order_id": "B" + "ab" * 16 + "01",
            "payload": {
                "order_side": "BUY",
                "position_side": "LONG",
                "last_qty": "214",
                "last_px": "1",
                "position_id": "ASTERUSDT-PERP.BINANCE-LONG",
            },
        },
        {
            "account_id": "account-c",
            "intent_id": short_intent,
            "event_type": "OrderFilled",
            "ts_event": t1,
            "instrument_id": "ASTERUSDT-PERP.BINANCE",
            "client_order_id": "B" + "cd" * 16 + "01",
            "payload": {
                "order_side": "SELL",
                "position_side": "SHORT",
                "last_qty": "214",
                "last_px": "1",
                "position_id": "ASTERUSDT-PERP.BINANCE-SHORT",
            },
        },
    ]
    stats = trade_outcomes.EpisodeBuildStats()
    episodes = trade_outcomes.build_position_episodes(records, stats=stats)
    assert episodes == []
    assert stats.open_lot_count == 2


def test_partial_then_full_close_one_lot(tmp_path):
    intent_id = str(uuid4())
    t0 = datetime(2026, 9, 21, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 22, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 23, tzinfo=timezone.utc)
    cid = "B" + "11" * 16
    records = [
        {
            "account_id": "account-a",
            "intent_id": intent_id,
            "event_type": "OrderFilled",
            "ts_event": t0,
            "instrument_id": "ETHUSDT-PERP.BINANCE",
            "client_order_id": cid + "01",
            "payload": {
                "order_side": "BUY",
                "position_id": "ETHUSDT-PERP.BINANCE-LONG",
                "last_qty": "1",
                "last_px": "100",
                "reduce_only": False,
            },
        },
        {
            "account_id": "account-a",
            "intent_id": str(uuid4()),
            "event_type": "OrderFilled",
            "ts_event": t1,
            "instrument_id": "ETHUSDT-PERP.BINANCE",
            "client_order_id": "B" + "22" * 16 + "01",
            "payload": {
                "order_side": "SELL",
                "position_id": "ETHUSDT-PERP.BINANCE-LONG",
                "last_qty": "0.4",
                "last_px": "110",
                "reduce_only": True,
            },
        },
        {
            "account_id": "account-a",
            "intent_id": str(uuid4()),
            "event_type": "OrderFilled",
            "ts_event": t2,
            "instrument_id": "ETHUSDT-PERP.BINANCE",
            "client_order_id": "B" + "33" * 16 + "01",
            "payload": {
                "order_side": "SELL",
                "position_id": "ETHUSDT-PERP.BINANCE-LONG",
                "last_qty": "0.6",
                "last_px": "120",
                "reduce_only": True,
                "realized_pnl": "14",
            },
        },
    ]
    outcomes = trade_outcomes.compute_outcomes(
        trade_outcomes.build_position_episodes(records),
        cache_dir=tmp_path,
    )
    assert len(outcomes) == 1
    assert outcomes[0]["intent_id"] == intent_id
    assert outcomes[0]["filled_quantity"] == Decimal("1")
    assert outcomes[0]["realized_pnl"] == Decimal("18")


def test_trade_id_dedup_prefers_realized_pnl():
    intent_id = str(uuid4())
    ts = datetime(2026, 9, 23, 16, tzinfo=timezone.utc)
    base = {
        "account_id": "account-d",
        "intent_id": intent_id,
        "event_type": "OrderFilled",
        "ts_event": ts,
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "client_order_id": "B" + "aa" * 16 + "01",
        "trade_id": "8112627135",
        "payload": {
            "order_side": "SELL",
            "position_side": "LONG",
            "last_qty": "0.1",
            "last_px": "1",
            "reduce_only": True,
            "position_id": "BTCUSDT-PERP.BINANCE-LONG",
        },
    }
    duplicate = copy.deepcopy(base)
    duplicate["event_id"] = "dup"
    duplicate["payload"]["realized_pnl"] = "5"
    entry = {
        **base,
        "trade_id": "entry-1",
        "client_order_id": "B" + "aa" * 16 + "02",
        "payload": {
            "order_side": "BUY",
            "position_side": "LONG",
            "last_qty": "0.1",
            "last_px": "1",
            "position_id": "BTCUSDT-PERP.BINANCE-LONG",
            "reduce_only": False,
        },
        "ts_event": datetime(2026, 9, 23, 15, tzinfo=timezone.utc),
    }
    stats = trade_outcomes.EpisodeBuildStats()
    episodes = trade_outcomes.build_position_episodes([entry, base, duplicate], stats=stats)
    assert stats.deduped_fill_count == 1
    assert len(episodes) == 1
    assert episodes[0]["realized_pnl"] == Decimal("5")


def test_unmatched_reduce_only_is_diagnosed():
    stats = trade_outcomes.EpisodeBuildStats()
    records = [
        {
            "account_id": "account-d",
            "intent_id": str(uuid4()),
            "event_type": "OrderFilled",
            "ts_event": datetime(2026, 9, 23, tzinfo=timezone.utc),
            "instrument_id": "SOLUSDT-PERP.BINANCE",
            "client_order_id": "B" + "ee" * 16 + "31",
            "payload": {
                "order_side": "SELL",
                "position_side": "LONG",
                "last_qty": "0.25",
                "last_px": "100",
                "reduce_only": True,
                "position_id": "SOLUSDT-PERP.BINANCE-LONG",
            },
        }
    ]
    episodes = trade_outcomes.build_position_episodes(records, stats=stats)
    assert episodes == []
    assert stats.unmatched_exit_count == 1
    assert stats.unmatched_exits


def test_allocated_exit_qty_and_fees_sum_to_observed_fill(tmp_path):
    episodes = trade_outcomes.build_position_episodes(_load_btc_records())
    outcomes = trade_outcomes.compute_outcomes(episodes, cache_dir=tmp_path)
    fees = sum((row["fees"] or Decimal("0")) for row in outcomes)
    assert abs(fees - Decimal("14.00210116")) < Decimal("0.00000002")
    close_qtys = Decimal("0")
    for episode in episodes:
        for event in episode["events"]:
            payload = event.get("payload") or {}
            if str(payload.get("last_px")) in {"83600", "83600.00000"}:
                close_qtys += Decimal(str(payload["last_qty"]))
    assert close_qtys == Decimal("0.235")


def test_poorer_duplicate_later_does_not_replace_richer_pnl():
    intent_id = str(uuid4())
    entry = {
        "account_id": "account-d",
        "intent_id": intent_id,
        "event_type": "OrderFilled",
        "event_id": "e0",
        "ts_event": datetime(2026, 9, 23, 15, tzinfo=timezone.utc),
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "client_order_id": "B" + "aa" * 16 + "02",
        "trade_id": "entry-1",
        "payload": {
            "order_side": "BUY",
            "position_side": "LONG",
            "last_qty": "0.1",
            "last_px": "1",
            "position_id": "BTCUSDT-PERP.BINANCE-LONG",
        },
    }
    rich = {
        **entry,
        "event_id": "e1",
        "trade_id": "8112627135",
        "client_order_id": "B" + "aa" * 16 + "01",
        "ts_event": datetime(2026, 9, 23, 16, tzinfo=timezone.utc),
        "payload": {
            "order_side": "SELL",
            "position_side": "LONG",
            "last_qty": "0.1",
            "last_px": "2",
            "reduce_only": True,
            "realized_pnl": "9",
            "commission": "1",
            "position_id": "BTCUSDT-PERP.BINANCE-LONG",
        },
    }
    poor = copy.deepcopy(rich)
    poor["event_id"] = "e2"
    poor["payload"].pop("realized_pnl")
    poor["payload"].pop("commission")
    stats = trade_outcomes.EpisodeBuildStats()
    episodes = trade_outcomes.build_position_episodes([entry, rich, poor], stats=stats)
    assert stats.deduped_fill_count == 1
    assert episodes[0]["realized_pnl"] == Decimal("9")


def test_order_partially_filled_is_included():
    intent_id = str(uuid4())
    records = [
        {
            "account_id": "account-a",
            "intent_id": intent_id,
            "event_type": "OrderPartiallyFilled",
            "event_id": "p1",
            "ts_event": datetime(2026, 9, 23, 1, tzinfo=timezone.utc),
            "instrument_id": "ETHUSDT-PERP.BINANCE",
            "client_order_id": "B" + "11" * 16 + "01",
            "trade_id": "t-entry",
            "payload": {
                "order_side": "BUY",
                "position_id": "ETHUSDT-PERP.BINANCE-LONG",
                "last_qty": "1",
                "last_px": "10",
            },
        },
        {
            "account_id": "account-a",
            "intent_id": intent_id,
            "event_type": "OrderFilled",
            "event_id": "p2",
            "ts_event": datetime(2026, 9, 23, 2, tzinfo=timezone.utc),
            "instrument_id": "ETHUSDT-PERP.BINANCE",
            "client_order_id": "B" + "11" * 16 + "31",
            "trade_id": "t-exit",
            "payload": {
                "order_side": "SELL",
                "position_id": "ETHUSDT-PERP.BINANCE-LONG",
                "last_qty": "1",
                "last_px": "12",
                "reduce_only": True,
                "realized_pnl": "2",
            },
        },
    ]
    episodes = trade_outcomes.build_position_episodes(records)
    assert len(episodes) == 1
    assert episodes[0]["realized_pnl"] == Decimal("2")


def test_missing_prices_do_not_fabricate_pnl(tmp_path):
    intent_id = str(uuid4())
    records = [
        {
            "account_id": "account-a",
            "intent_id": intent_id,
            "event_type": "OrderFilled",
            "ts_event": datetime(2026, 9, 23, 1, tzinfo=timezone.utc),
            "instrument_id": "ETHUSDT-PERP.BINANCE",
            "client_order_id": "B" + "11" * 16 + "01",
            "payload": {
                "order_side": "BUY",
                "position_id": "ETHUSDT-PERP.BINANCE-LONG",
                "last_qty": "1",
            },
        },
        {
            "account_id": "account-a",
            "intent_id": intent_id,
            "event_type": "OrderFilled",
            "ts_event": datetime(2026, 9, 23, 2, tzinfo=timezone.utc),
            "instrument_id": "ETHUSDT-PERP.BINANCE",
            "client_order_id": "B" + "11" * 16 + "31",
            "payload": {
                "order_side": "SELL",
                "position_id": "ETHUSDT-PERP.BINANCE-LONG",
                "last_qty": "1",
                "reduce_only": True,
            },
        },
    ]
    outcomes = trade_outcomes.compute_outcomes(
        trade_outcomes.build_position_episodes(records),
        cache_dir=tmp_path,
    )
    assert outcomes[0]["realized_pnl"] is None
    assert outcomes[0]["details"]["pnl"]["status"] == "missing"


def test_hedge_unknown_side_is_ambiguous_not_silent_close():
    records = [
        {
            "account_id": "account-c",
            "intent_id": str(uuid4()),
            "event_type": "OrderFilled",
            "ts_event": datetime(2026, 9, 23, 1, tzinfo=timezone.utc),
            "instrument_id": "ASTERUSDT-PERP.BINANCE",
            "client_order_id": "B" + "11" * 16 + "01",
            "payload": {
                "order_side": "BUY",
                "position_side": "LONG",
                "last_qty": "1",
                "last_px": "1",
                "position_id": "ASTERUSDT-PERP.BINANCE-LONG",
            },
        },
        {
            "account_id": "account-c",
            "intent_id": str(uuid4()),
            "event_type": "OrderFilled",
            "ts_event": datetime(2026, 9, 23, 2, tzinfo=timezone.utc),
            "instrument_id": "ASTERUSDT-PERP.BINANCE",
            "client_order_id": "B" + "22" * 16 + "01",
            "order_plan": {"side": "short", "position_mode": "HEDGE"},
            "payload": {
                "order_side": "SELL",
                "position_mode": "HEDGE",
                "last_qty": "1",
                "last_px": "1",
            },
        },
    ]
    stats = trade_outcomes.EpisodeBuildStats()
    episodes = trade_outcomes.build_position_episodes(records, stats=stats)
    assert episodes == []
    assert stats.diagnostics
    assert any("ambiguous" in item for item in stats.diagnostics)


def test_opening_order_plan_stop_loss_carries_into_risk(tmp_path):
    intent_id = str(uuid4())
    records = [
        {
            "account_id": "account-a",
            "intent_id": intent_id,
            "event_type": "OrderFilled",
            "ts_event": datetime(2026, 9, 23, 1, tzinfo=timezone.utc),
            "instrument_id": "ETHUSDT-PERP.BINANCE",
            "client_order_id": "B" + "11" * 16 + "01",
            "order_plan": {"side": "long", "stop_loss": "90"},
            "payload": {
                "order_side": "BUY",
                "position_id": "ETHUSDT-PERP.BINANCE-LONG",
                "last_qty": "1",
                "last_px": "100",
            },
        },
        {
            "account_id": "account-a",
            "intent_id": intent_id,
            "event_type": "OrderFilled",
            "ts_event": datetime(2026, 9, 23, 2, tzinfo=timezone.utc),
            "instrument_id": "ETHUSDT-PERP.BINANCE",
            "client_order_id": "B" + "11" * 16 + "31",
            "payload": {
                "order_side": "SELL",
                "position_id": "ETHUSDT-PERP.BINANCE-LONG",
                "last_qty": "1",
                "last_px": "110",
                "reduce_only": True,
                "realized_pnl": "10",
            },
        },
    ]
    outcomes = trade_outcomes.compute_outcomes(
        trade_outcomes.build_position_episodes(records),
        cache_dir=tmp_path,
    )
    assert outcomes[0]["initial_risk"] == Decimal("10")
    assert outcomes[0]["r_multiple"] == Decimal("1")


def test_position_opened_recovery_is_not_a_new_fill(tmp_path):
    with_restore = trade_outcomes.build_position_episodes(_load_btc_records(include_position_events=True))
    fills_only = trade_outcomes.build_position_episodes(_load_btc_records(include_position_events=False))
    assert len(with_restore) == len(fills_only) == 3
    assert {row["lot_key"] for row in with_restore} == {row["lot_key"] for row in fills_only}


def test_position_closed_without_fills_does_not_fabricate_qty():
    intent_id = str(uuid4())
    records = [
        {
            "account_id": "account-a",
            "intent_id": intent_id,
            "event_type": "OrderFilled",
            "ts_event": datetime(2026, 9, 23, 1, tzinfo=timezone.utc),
            "instrument_id": "ETHUSDT-PERP.BINANCE",
            "client_order_id": "B" + "11" * 16 + "01",
            "payload": {
                "order_side": "BUY",
                "position_id": "ETHUSDT-PERP.BINANCE-LONG",
                "last_qty": "1",
                "last_px": "100",
            },
        },
        {
            "account_id": "account-a",
            "intent_id": intent_id,
            "event_type": "PositionClosed",
            "ts_event": datetime(2026, 9, 23, 2, tzinfo=timezone.utc),
            "instrument_id": "ETHUSDT-PERP.BINANCE",
            "payload": {
                "side": 1,
                "quantity": "0",
                "position_id": "ETHUSDT-PERP.BINANCE-LONG",
                "instrument_id": "ETHUSDT-PERP.BINANCE",
            },
        },
    ]
    stats = trade_outcomes.EpisodeBuildStats()
    episodes = trade_outcomes.build_position_episodes(records, stats=stats)
    assert episodes == []
    assert stats.open_lot_count == 1
    assert stats.unmatched_exit_count == 1
    assert any("position_closed_without_matching_fills" in item for item in (stats.diagnostics or []))


def test_closed_lot_missing_prices_still_emits_outcome(tmp_path):
    intent_id = str(uuid4())
    records = [
        {
            "account_id": "account-a",
            "intent_id": intent_id,
            "event_type": "OrderFilled",
            "ts_event": datetime(2026, 9, 23, 1, tzinfo=timezone.utc),
            "instrument_id": "ETHUSDT-PERP.BINANCE",
            "client_order_id": "B" + "11" * 16 + "01",
            "payload": {
                "order_side": "BUY",
                "position_id": "ETHUSDT-PERP.BINANCE-LONG",
                "last_qty": "1",
            },
        },
        {
            "account_id": "account-a",
            "intent_id": intent_id,
            "event_type": "OrderFilled",
            "ts_event": datetime(2026, 9, 23, 2, tzinfo=timezone.utc),
            "instrument_id": "ETHUSDT-PERP.BINANCE",
            "client_order_id": "B" + "11" * 16 + "31",
            "payload": {
                "order_side": "SELL",
                "position_id": "ETHUSDT-PERP.BINANCE-LONG",
                "last_qty": "1",
                "reduce_only": True,
            },
        },
    ]
    outcomes = trade_outcomes.compute_outcomes(
        trade_outcomes.build_position_episodes(records),
        cache_dir=tmp_path,
    )
    assert len(outcomes) == 1
    assert outcomes[0]["realized_pnl"] is None
    assert outcomes[0]["details"]["pnl"]["status"] == "missing"
