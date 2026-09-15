from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from strategy.intent_execution_strategy import (  # noqa: E402
    _book_id_quantity,
    _hedge_cache_blocks_reduce_only,
)


INSTRUMENT_ID = "ALGOUSDT-PERP.BINANCE"


def _pos(position_id: str, quantity: str, side: str = "LONG") -> SimpleNamespace:
    return SimpleNamespace(
        id=position_id,
        position_id=position_id,
        quantity=quantity,
        side=side,
    )


class HedgeReduceOnlySubmissionTest(unittest.TestCase):
    def test_empty_cache_blocks_reduce_only(self) -> None:
        self.assertTrue(
            _hedge_cache_blocks_reduce_only(
                INSTRUMENT_ID, "SELL", "2374.2", (),
            )
        )

    def test_undersized_book_id_blocks_reduce_only(self) -> None:
        positions = (
            _pos(f"{INSTRUMENT_ID}-LONG", "1302.1"),
        )
        self.assertTrue(
            _hedge_cache_blocks_reduce_only(
                INSTRUMENT_ID, "SELL", "2374.2", positions,
            )
        )
        self.assertEqual(
            _book_id_quantity(INSTRUMENT_ID, "LONG", positions),
            __import__("decimal").Decimal("1302.1"),
        )

    def test_external_only_cache_blocks_reduce_only(self) -> None:
        positions = (
            _pos(f"{INSTRUMENT_ID}-EXTERNAL", "11871.2"),
        )
        self.assertTrue(
            _hedge_cache_blocks_reduce_only(
                INSTRUMENT_ID, "SELL", "2374.2", positions,
            )
        )
        self.assertEqual(
            _book_id_quantity(INSTRUMENT_ID, "LONG", positions),
            __import__("decimal").Decimal("0"),
        )

    def test_sufficient_book_id_keeps_reduce_only(self) -> None:
        positions = (
            _pos(f"{INSTRUMENT_ID}-LONG", "11871.2"),
        )
        self.assertFalse(
            _hedge_cache_blocks_reduce_only(
                INSTRUMENT_ID, "SELL", "2374.2", positions,
            )
        )

    def test_buy_reduce_uses_short_book(self) -> None:
        positions = (
            _pos(f"{INSTRUMENT_ID}-SHORT", "10", side="SHORT"),
        )
        self.assertFalse(
            _hedge_cache_blocks_reduce_only(
                INSTRUMENT_ID, "BUY", "4", positions,
            )
        )
        self.assertTrue(
            _hedge_cache_blocks_reduce_only(
                INSTRUMENT_ID, "BUY", "11", positions,
            )
        )


if __name__ == "__main__":
    unittest.main()
