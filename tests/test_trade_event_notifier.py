from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "trade_event_notifier.py"
SPEC = importlib.util.spec_from_file_location("trade_event_notifier", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
notifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notifier)


def _state():
    return notifier.ensure_state({})


def _intent_row(event_type="intent_ack.accepted"):
    return {
        "source_id": "audit-1",
        "event_type": event_type,
        "intent_id": "b9a12bcd-e1e3-4db5-89b2-dae9a9f719a8",
        "account_id": "account-a",
        "instrument_id": "MUUSDT",
        "action": "open_position",
        "order_plan": {
            "side": "long",
            "entry": {"price_min": 845.845, "price_max": 865.865},
        },
        "risk_budget": {"max_notional": 1859.013},
        "payload": {"detail": None},
    }


def _accepted_order(client_order_id, action="open_position", created_by="hermes-agent"):
    row = _intent_row()
    row.update(
        {
            "source_id": "event-" + client_order_id,
            "event_type": "OrderAccepted",
            "client_order_id": client_order_id,
            "action": action,
            "order_plan": {
                "side": "long",
                "entry": {"price_min": 845.845, "price_max": 865.865},
                "stop_loss": 821.7,
                "take_profits": [899.1, 929.07],
                "authorization": {"created_by_service": created_by},
            },
        }
    )
    return row


class TradeEventNotifierTests(unittest.TestCase):
    def test_intent_message_contains_required_fields(self):
        text = notifier.format_intent_message(_intent_row())

        self.assertIn("Intent 已批准", text)
        self.assertIn("account-a", text)
        self.assertIn("MUUSDT", text)
        self.assertIn("做多", text)
        self.assertIn("1859.01 USDT", text)

    def test_rejection_reason_and_semantic_dedupe(self):
        row = _intent_row("intent_ack.rejected")
        row["payload"] = {
            "detail": "denied:position_required:BTCUSDT-PERP.BINANCE-SHORT"
        }
        state = _state()
        key = notifier.intent_dedupe_key(row)

        self.assertIn("缺少对应持仓", notifier.format_intent_message(row))
        self.assertTrue(
            notifier.enqueue_message(state, "intent", key, "first", 10.0)
        )
        self.assertFalse(
            notifier.enqueue_message(state, "intent", key, "replay", 11.0)
        )

    def test_manual_order_ids_are_excluded(self):
        self.assertTrue(
            notifier.is_robot_order_id(
                "Bb9a12bcde1e34db589b2dae9a9f719a801"
            )
        )
        self.assertFalse(
            notifier.is_robot_order_id("aos_HOCb8zoV9dBXxL3gOmTQ")
        )
        self.assertFalse(
            notifier.is_robot_order_id("stToAg_OTO_632643320_2")
        )

    def test_entry_ladder_batches_to_one_message(self):
        state = _state()
        rows = [
            _accepted_order("Bb9a12bcde1e34db589b2dae9a9f719a801"),
            _accepted_order("Bb9a12bcde1e34db589b2dae9a9f719a802"),
            _accepted_order("Bb9a12bcde1e34db589b2dae9a9f719a803"),
        ]
        for row in rows:
            self.assertTrue(notifier.add_order_batch(state, row, 10.0))

        exchange_orders = {}
        prices = ("865.87", "855.86", "848.85")
        quantities = ("1", "0.71", "0.44")
        for row, price, quantity in zip(rows, prices, quantities):
            key = "account-a:" + row["client_order_id"]
            exchange_orders[key] = {
                "price": price,
                "quantity": quantity,
                "type": "LIMIT",
            }

        count = notifier.flush_order_batches(
            state,
            20.0,
            exchange_orders=exchange_orders,
        )

        self.assertEqual(count, 1)
        self.assertEqual(len(state["pending"]), 1)
        text = state["pending"][0]["text"]
        self.assertIn("阶梯 3 档", text)
        self.assertIn("总数量 2.15", text)
        self.assertIn("848.85～865.87", text)

    def test_protection_and_repair_classification(self):
        initial = _accepted_order(
            "Bb9a12bcde1e34db589b2dae9a9f719a811"
        )
        repair = _accepted_order(
            "Bb9a12bcde1e34db589b2dae9a9f719a821"
        )
        lifecycle_repair = _accepted_order(
            "Bb9a12bcde1e34db589b2dae9a9f719a801",
            action="move_stop_loss",
            created_by="order-lifecycle-monitor",
        )

        self.assertEqual(
            notifier.classify_order(initial),
            ("protection", False),
        )
        self.assertEqual(
            notifier.classify_order(repair),
            ("protection", True),
        )
        self.assertEqual(
            notifier.classify_order(lifecycle_repair),
            ("protection", True),
        )

    def test_protection_batch_formats_initial_and_repair(self):
        initial_state = _state()
        initial = _accepted_order(
            "Bb9a12bcde1e34db589b2dae9a9f719a811"
        )
        notifier.add_order_batch(initial_state, initial, 10.0)
        initial_key = "account-a:" + initial["client_order_id"]
        initial_exchange = {
            initial_key: {
                "type": "STOP_MARKET",
                "trigger_price": "821.7",
                "quantity": "2.15",
            }
        }

        notifier.flush_order_batches(
            initial_state,
            20.0,
            exchange_orders=initial_exchange,
        )

        initial_text = initial_state["pending"][0]["text"]
        self.assertIn("保护单已挂出", initial_text)
        self.assertIn("止损 821.7", initial_text)

        repair_state = _state()
        repair = _accepted_order(
            "Bb9a12bcde1e34db589b2dae9a9f719a821"
        )
        notifier.add_order_batch(repair_state, repair, 10.0)
        repair_key = "account-a:" + repair["client_order_id"]
        repair_exchange = {
            repair_key: {
                "type": "STOP_MARKET",
                "trigger_price": "825",
                "quantity": "2.15",
            }
        }

        notifier.flush_order_batches(
            repair_state,
            20.0,
            exchange_orders=repair_exchange,
        )

        self.assertIn("保护单已补挂", repair_state["pending"][0]["text"])

    def test_fill_message_is_per_trade(self):
        row = _intent_row()
        row.update(
            {
                "event_type": "OrderFilled",
                "trade_id": "8593514661",
                "payload": {"last_qty": "0.5", "last_px": "850.25"},
            }
        )

        text = notifier.format_fill_message(row)

        self.assertIn("成交", text)
        self.assertIn("0.5 @ 850.25", text)
        self.assertIn("425.13 USDT", text)
        self.assertIn("8593514661", text)

    def test_delivery_failure_schedules_retry_without_raising(self):
        state = _state()
        notifier.enqueue_message(state, "fill", "fill:1", "成交", 10.0)

        def failing_sender(_text):
            return False, {"error": "telegram_url_error"}

        with patch.object(notifier, "RETRY_BASE_SECONDS", 5.0):
            stats = notifier.process_pending(
                state,
                sender=failing_sender,
                now_ts=10.0,
            )

        self.assertEqual(
            stats,
            {
                "sent": 0,
                "retried": 1,
                "degraded": 0,
                "rate_limited": 0,
            },
        )
        self.assertEqual(len(state["pending"]), 1)
        self.assertEqual(state["pending"][0]["attempts"], 1)
        self.assertEqual(state["pending"][0]["next_attempt_at"], 15.0)

    def test_delivery_exhaustion_records_degraded_dead_letter(self):
        state = _state()
        notifier.enqueue_message(state, "fill", "fill:1", "成交", 10.0)

        def failing_sender(_text):
            return False, {"error": "telegram_url_error"}

        with patch.object(notifier, "MAX_DELIVERY_ATTEMPTS", 2):
            first = notifier.process_pending(
                state,
                sender=failing_sender,
                now_ts=10.0,
            )
            second = notifier.process_pending(
                state,
                sender=failing_sender,
                now_ts=15.0,
            )

        self.assertEqual(first["retried"], 1)
        self.assertEqual(second["degraded"], 1)
        self.assertEqual(state["pending"], [])
        self.assertEqual(len(state["dead_letters"]), 1)
        self.assertEqual(state["dead_letters"][0]["attempts"], 2)

    def test_per_class_rate_limit_defers_excess(self):
        state = _state()
        notifier.enqueue_message(state, "fill", "fill:1", "first", 10.0)
        notifier.enqueue_message(state, "fill", "fill:2", "second", 10.0)

        def sender(text):
            return True, {"message_id": text, "text": text}

        with patch.object(notifier, "PER_CLASS_PER_MINUTE", 1):
            stats = notifier.process_pending(
                state,
                sender=sender,
                now_ts=10.0,
            )

        self.assertEqual(stats["sent"], 1)
        self.assertEqual(stats["rate_limited"], 1)
        self.assertEqual(len(state["pending"]), 1)
        self.assertEqual(state["pending"][0]["text"], "second")

    def test_send_test_uses_delivery_queue(self):
        sent = []

        def sender(text):
            sent.append(text)
            return True, {"message_id": 123, "text": text}

        exit_code = notifier.send_test(sender=sender)

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(sent), 1)
        self.assertIn("安全模拟事件", sent[0])


if __name__ == "__main__":
    unittest.main()
