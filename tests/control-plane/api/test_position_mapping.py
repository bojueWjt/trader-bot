from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [
    str(_REPO / "services" / "control-plane" / "api"),
    str(_REPO / "packages" / "execution-domain"),
]

from position_mapping import (
    annotate_with_projection,
    canonical_position_id,
    hedge_book,
    open_books_from_mirror_payload,
    position_ids_equivalent,
)


class PositionMappingTest(unittest.TestCase):
    def test_hedge_book_prefers_explicit_side_over_unsigned_qty(self) -> None:
        self.assertEqual(hedge_book("SHORT", "2.0"), "short")
        self.assertEqual(hedge_book("LONG", "-9.89"), "long")
        self.assertEqual(hedge_book("BOTH", "1.25"), "long")
        self.assertEqual(hedge_book("BOTH", "-0.027"), "short")
        self.assertIsNone(hedge_book("BOTH", "0"))

    def test_canonical_id_is_nautilus_instrument_plus_book(self) -> None:
        self.assertEqual(
            canonical_position_id("TAOUSDT", "long"),
            "TAOUSDT-PERP.BINANCE-LONG",
        )
        self.assertEqual(
            canonical_position_id("BTCUSDT-PERP.BINANCE", "short"),
            "BTCUSDT-PERP.BINANCE-SHORT",
        )

    def test_external_and_both_match_resolved_long_short(self) -> None:
        self.assertTrue(
            position_ids_equivalent(
                "TAOUSDT-PERP.BINANCE-EXTERNAL",
                "TAOUSDT-PERP.BINANCE-LONG",
                side="long",
            )
        )
        self.assertTrue(
            position_ids_equivalent(
                "BTCUSDT-PERP.BINANCE-BOTH",
                "BTCUSDT-PERP.BINANCE-SHORT",
                side="short",
            )
        )
        self.assertFalse(
            position_ids_equivalent(
                "TAOUSDT-PERP.BINANCE-EXTERNAL",
                "TAOUSDT-PERP.BINANCE-LONG",
                side="short",
            )
        )
        self.assertFalse(
            position_ids_equivalent(
                "ETHUSDT-PERP.BINANCE-LONG",
                "BTCUSDT-PERP.BINANCE-LONG",
                side="long",
            )
        )

    def test_mirror_skips_flat_and_maps_signed_both(self) -> None:
        rows = open_books_from_mirror_payload(
            "account-c",
            {
                "positions": [
                    {
                        "symbol": "TAOUSDT",
                        "position_side": "LONG",
                        "position_amt": "9.890",
                    },
                    {
                        "symbol": "BTCUSDT",
                        "position_side": "SHORT",
                        "position_amt": "-0.027",
                    },
                    {
                        "symbol": "ZECUSDT",
                        "position_side": "BOTH",
                        "position_amt": "0",
                    },
                    {
                        "symbol": "ETHUSDT_260925",
                        "position_side": "BOTH",
                        "position_amt": "-1.5",
                    },
                ]
            },
        )
        ids = {row["position_id"]: row for row in rows}
        self.assertEqual(
            set(ids),
            {
                "TAOUSDT-PERP.BINANCE-LONG",
                "BTCUSDT-PERP.BINANCE-SHORT",
                "ETHUSDT_260925-PERP.BINANCE-SHORT",
            },
        )
        self.assertEqual(ids["TAOUSDT-PERP.BINANCE-LONG"]["quantity"], "9.89")
        self.assertEqual(ids["BTCUSDT-PERP.BINANCE-SHORT"]["side"], "short")

    def test_venue_quantity_wins_over_stale_projection(self) -> None:
        venue = open_books_from_mirror_payload(
            "account-c",
            {
                "positions": [
                    {
                        "symbol": "TAOUSDT",
                        "position_side": "LONG",
                        "position_amt": "9.890",
                        "entry_price": "231.83",
                    }
                ]
            },
        )[0]
        merged = annotate_with_projection(
            venue,
            [
                {
                    "account_id": "account-c",
                    "instrument_id": "TAOUSDT-PERP.BINANCE",
                    "side": "long",
                    "position_id": "TAOUSDT-PERP.BINANCE-LONG",
                    "quantity": "0.308",
                    "payload": {"intent_id": "intent-tao", "stop_loss": 200},
                },
                {
                    "account_id": "account-c",
                    "instrument_id": "ZECUSDT-PERP.BINANCE",
                    "side": "short",
                    "position_id": "ZECUSDT-PERP.BINANCE-SHORT",
                    "quantity": "0.864",
                    "payload": {},
                },
            ],
        )
        self.assertEqual(merged["quantity"], "9.89")
        self.assertEqual(merged["intent_id"], "intent-tao")
        self.assertEqual(merged["stop_loss"], 200)
        self.assertNotIn("ZEC", merged["position_id"])


if __name__ == "__main__":
    unittest.main()
