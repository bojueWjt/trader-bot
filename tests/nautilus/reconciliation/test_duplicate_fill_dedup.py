from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
sys.path.insert(0, str(SERVICE_ROOT))

from persistence.idempotency import FillIdentity, InMemoryIdempotencyStore  # noqa: E402


class DuplicateFillDedupTests(unittest.TestCase):
    def test_duplicate_fill_is_accepted_once_per_account(self) -> None:
        store = InMemoryIdempotencyStore()
        fill = FillIdentity(
            account_id="account-a",
            venue_order_id="binance-order-1",
            trade_id="trade-1",
            event_kind="fill",
        )

        self.assertTrue(store.claim_fill(fill))
        self.assertFalse(store.claim_fill(fill))
        self.assertEqual(store.claim_count, 1)

    def test_reconciliation_replay_uses_same_fill_key(self) -> None:
        store = InMemoryIdempotencyStore()
        websocket_fill = FillIdentity(
            account_id="account-a",
            venue_order_id="binance-order-1",
            trade_id="trade-1",
            event_kind="fill",
            source="websocket",
        )
        replayed_fill = FillIdentity(
            account_id="account-a",
            venue_order_id="binance-order-1",
            trade_id="trade-1",
            event_kind="fill",
            source="reconciliation",
        )

        self.assertTrue(store.claim_fill(websocket_fill))
        self.assertFalse(store.claim_fill(replayed_fill))

    def test_same_exchange_trade_id_is_isolated_by_account(self) -> None:
        store = InMemoryIdempotencyStore()
        account_a_fill = FillIdentity(
            account_id="account-a",
            venue_order_id="binance-order-1",
            trade_id="trade-1",
            event_kind="fill",
        )
        account_b_fill = FillIdentity(
            account_id="account-b",
            venue_order_id="binance-order-1",
            trade_id="trade-1",
            event_kind="fill",
        )

        self.assertTrue(store.claim_fill(account_a_fill))
        self.assertTrue(store.claim_fill(account_b_fill))
        self.assertEqual(store.claim_count, 2)


if __name__ == "__main__":
    unittest.main()
