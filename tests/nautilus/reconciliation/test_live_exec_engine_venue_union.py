"""WP-D tests: startup reconciliation scope is risk keys UNION venue instruments.

Contract: docs/plans/2026-08-28-execution-state-arch-migration.md (WP-D).
build_live_exec_engine_kwargs grows an optional venue_instrument_ids parameter;
the final reconciliation_instrument_ids is
sorted(set(risk keys) | set(venue_instrument_ids or ())), still excluding
DEFAULT_LIVE_ENTRY_NOTIONAL_KEY. Invariant: a venue-held instrument absent
from the risk whitelist (the ATOM incident shape) must be reconciled.
"""

from __future__ import annotations

import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
sys.path.insert(0, str(SERVICE_ROOT))

from config.node_config import (  # noqa: E402
    RiskNodeConfig,
    load_node_config,
)
from persistence.nautilus_config import (  # noqa: E402
    DEFAULT_LIVE_ENTRY_NOTIONAL_KEY,
    build_live_exec_engine_kwargs,
)


RISK_ONLY_IDS = ["BTCUSDT-PERP.BINANCE", "ETHUSDT-PERP.BINANCE"]
ATOM_ID = "ATOMUSDT-PERP.BINANCE"


class LiveExecEngineVenueUnionTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["BINANCE_ACCOUNT_A_API_KEY"] = "account-a-key"
        os.environ["BINANCE_ACCOUNT_A_API_SECRET"] = "account-a-secret"
        os.environ["CONTROL_PLANE_ACCOUNT_A_TOKEN"] = "node-a-token"

    def _live_account(self):
        account = load_node_config(
            SERVICE_ROOT / "config" / "examples" / "account-a.sandbox.json"
        )
        return replace(
            account,
            binance=replace(account.binance, environment="live"),
            risk=RiskNodeConfig(
                max_notional_per_order={
                    DEFAULT_LIVE_ENTRY_NOTIONAL_KEY: "10",
                    "ETHUSDT-PERP.BINANCE": "100",
                    "BTCUSDT-PERP.BINANCE": "100",
                },
                max_order_submit_rate="50/00:00:01",
                max_order_modify_rate="1/00:00:01",
            ),
        )

    def test_venue_position_outside_risk_whitelist_joins_reconciliation_scope(
        self,
    ) -> None:
        """ATOM incident invariant: venue holds ATOM, risk keys lack ATOM."""
        kwargs = build_live_exec_engine_kwargs(
            self._live_account(),
            venue_instrument_ids=[ATOM_ID],
        )

        self.assertEqual(
            kwargs["reconciliation_instrument_ids"],
            sorted([ATOM_ID, *RISK_ONLY_IDS]),
        )
        self.assertNotIn(
            DEFAULT_LIVE_ENTRY_NOTIONAL_KEY,
            kwargs["reconciliation_instrument_ids"],
        )

    def test_none_venue_instrument_ids_falls_back_to_risk_keys(self) -> None:
        account = self._live_account()

        with_none = build_live_exec_engine_kwargs(
            account,
            venue_instrument_ids=None,
        )
        legacy_call = build_live_exec_engine_kwargs(account)

        self.assertEqual(
            with_none["reconciliation_instrument_ids"],
            RISK_ONLY_IDS,
        )
        self.assertEqual(with_none, legacy_call)

    def test_empty_venue_iterable_is_equivalent_to_none(self) -> None:
        kwargs = build_live_exec_engine_kwargs(
            self._live_account(),
            venue_instrument_ids=(),
        )

        self.assertEqual(
            kwargs["reconciliation_instrument_ids"],
            RISK_ONLY_IDS,
        )

    def test_union_deduplicates_and_sorts_overlapping_instrument_ids(self) -> None:
        kwargs = build_live_exec_engine_kwargs(
            self._live_account(),
            venue_instrument_ids=[
                "BTCUSDT-PERP.BINANCE",
                ATOM_ID,
                ATOM_ID,
            ],
        )

        self.assertEqual(
            kwargs["reconciliation_instrument_ids"],
            sorted([ATOM_ID, *RISK_ONLY_IDS]),
        )


if __name__ == "__main__":
    unittest.main()
