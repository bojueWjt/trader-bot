from __future__ import annotations

import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4


REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(PACKAGE_ROOT))

from execution_domain.account_execution_ledger import (  # noqa: E402
    BookKey,
    PositionState,
    ReconciledExecutionState,
    semantic_operation_id,
)


ACCOUNT_ID = "account-a"
ATOM_INSTRUMENT = "ATOMUSDT-PERP.BINANCE"
ATOM_SYMBOL = "ATOMUSDT"
NOW = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)
FRESH_AT = NOW - timedelta(seconds=10)
WINDOW = timedelta(seconds=30)

ATOM_LONG = BookKey(
    account_id=ACCOUNT_ID,
    instrument_id=ATOM_INSTRUMENT,
    position_side="LONG",
)
ATOM_SHORT = BookKey(
    account_id=ACCOUNT_ID,
    instrument_id=ATOM_INSTRUMENT,
    position_side="SHORT",
)


def _venue_position(
    symbol: str = ATOM_SYMBOL,
    position_amt: str = "596.48",
    position_side: str = "LONG",
) -> dict:
    return {
        "symbol": symbol,
        "position_amt": position_amt,
        "entry_price": "4.30",
        "mark_price": "4.35",
        "unrealized_pnl": "0",
        "position_side": position_side,
    }


def _venue_order(
    symbol: str = ATOM_SYMBOL,
    position_side: str = "LONG",
    client_order_id: str | None = None,
    order_kind: str = "regular",
) -> dict:
    return {
        "symbol": symbol,
        "position_side": position_side,
        "side": "SELL",
        "type": "TAKE_PROFIT_MARKET",
        "quantity": "10",
        "price": "0",
        "trigger_price": "4.80",
        "reduce_only": True,
        "client_order_id": client_order_id,
        "order_kind": order_kind,
        "venue_order_id": 123456,
        "order_id": 123456,
    }


def _venue_snapshot(
    positions: list[dict] | None = None,
    open_orders: list[dict] | None = None,
    algo_orders: list[dict] | None = None,
) -> dict:
    return {
        "source": "binance_fapi",
        "fetched_at": "2026-08-28T12:00:00Z",
        "positions": list(positions or []),
        "open_orders": list(open_orders or []),
        "algo_orders": list(algo_orders or []),
        "protections": [],
    }


def _cache_position(
    instrument_id: str = ATOM_INSTRUMENT,
    side: str = "LONG",
    quantity: str = "596.48",
    position_id: str | None = None,
) -> Any:
    return SimpleNamespace(
        instrument_id=instrument_id,
        side=side,
        quantity=quantity,
        position_id=position_id or f"{instrument_id}-{side}",
    )


def _build(
    venue_snapshot: dict | None,
    cache_positions: list[Any] | None = None,
    venue_fetched_at: datetime | None = FRESH_AT,
    now: datetime = NOW,
    freshness_window: timedelta = WINDOW,
) -> ReconciledExecutionState:
    return ReconciledExecutionState.build(
        account_id=ACCOUNT_ID,
        venue_snapshot=venue_snapshot,
        venue_fetched_at=venue_fetched_at,
        cache_positions=cache_positions or [],
        now=now,
        freshness_window=freshness_window,
    )


def _bot_client_order_id(sequence: int = 1) -> str:
    return f"B{uuid4().hex}{sequence:02d}"


class AtomIncidentRegressionTest(unittest.TestCase):
    """ATOM 事故形态：cache 视图为空但交易所有真实仓位。"""

    def test_cache_empty_venue_open_is_known_open(self) -> None:
        state = _build(
            _venue_snapshot(positions=[_venue_position(position_amt="596.48")]),
            cache_positions=[],
        )

        assessment = state.assess(ATOM_LONG)

        self.assertEqual(assessment.state, PositionState.KNOWN_OPEN)
        self.assertEqual(assessment.quantity, Decimal("596.48"))
        self.assertTrue(assessment.venue_fresh)

    def test_cache_empty_venue_open_other_side_stays_flat(self) -> None:
        state = _build(
            _venue_snapshot(positions=[_venue_position(position_amt="596.48")]),
            cache_positions=[],
        )

        assessment = state.assess(ATOM_SHORT)

        self.assertEqual(assessment.state, PositionState.KNOWN_FLAT)
        self.assertEqual(assessment.quantity, Decimal("0"))


class HedgeModeIndependenceTest(unittest.TestCase):
    """hedge mode 下同一 symbol 的 LONG/SHORT 是独立 book。"""

    def test_both_sides_open_assessed_independently(self) -> None:
        state = _build(
            _venue_snapshot(
                positions=[
                    _venue_position(position_amt="596.48", position_side="LONG"),
                    _venue_position(position_amt="-120", position_side="SHORT"),
                ]
            ),
            cache_positions=[],
        )

        long_assessment = state.assess(ATOM_LONG)
        short_assessment = state.assess(ATOM_SHORT)

        self.assertEqual(long_assessment.state, PositionState.KNOWN_OPEN)
        self.assertEqual(long_assessment.quantity, Decimal("596.48"))
        self.assertEqual(short_assessment.state, PositionState.KNOWN_OPEN)
        self.assertEqual(abs(short_assessment.quantity), Decimal("120"))

    def test_long_open_short_flat_do_not_bleed(self) -> None:
        state = _build(
            _venue_snapshot(
                positions=[_venue_position(position_amt="596.48", position_side="LONG")]
            ),
            cache_positions=[_cache_position(side="LONG", quantity="596.48")],
        )

        self.assertEqual(state.assess(ATOM_LONG).state, PositionState.KNOWN_OPEN)
        self.assertEqual(state.assess(ATOM_SHORT).state, PositionState.KNOWN_FLAT)

    def test_short_phantom_does_not_conflict_long_book(self) -> None:
        # cache 幻影仓在 SHORT book 上，LONG book 判定不受污染。
        state = _build(
            _venue_snapshot(
                positions=[_venue_position(position_amt="596.48", position_side="LONG")]
            ),
            cache_positions=[_cache_position(side="SHORT", quantity="50")],
        )

        self.assertEqual(state.assess(ATOM_LONG).state, PositionState.KNOWN_OPEN)
        self.assertEqual(state.assess(ATOM_SHORT).state, PositionState.CONFLICTED)


class FourStatePathsTest(unittest.TestCase):
    def test_known_open_matches_venue_quantity(self) -> None:
        state = _build(
            _venue_snapshot(positions=[_venue_position(position_amt="12.5")]),
            cache_positions=[_cache_position(quantity="12.5")],
        )

        assessment = state.assess(ATOM_LONG)

        self.assertEqual(assessment.state, PositionState.KNOWN_OPEN)
        self.assertEqual(assessment.quantity, Decimal("12.5"))
        self.assertTrue(assessment.venue_fresh)

    def test_known_flat_when_venue_absent_and_cache_empty(self) -> None:
        state = _build(_venue_snapshot(positions=[]), cache_positions=[])

        assessment = state.assess(ATOM_LONG)

        self.assertEqual(assessment.state, PositionState.KNOWN_FLAT)
        self.assertEqual(assessment.quantity, Decimal("0"))
        self.assertTrue(assessment.venue_fresh)

    def test_known_flat_when_venue_reports_explicit_zero(self) -> None:
        state = _build(
            _venue_snapshot(positions=[_venue_position(position_amt="0")]),
            cache_positions=[],
        )

        self.assertEqual(state.assess(ATOM_LONG).state, PositionState.KNOWN_FLAT)

    def test_unknown_when_snapshot_missing(self) -> None:
        state = _build(None, cache_positions=[_cache_position()])

        assessment = state.assess(ATOM_LONG)

        self.assertEqual(assessment.state, PositionState.UNKNOWN)
        self.assertEqual(assessment.quantity, Decimal("0"))
        self.assertFalse(assessment.venue_fresh)

    def test_unknown_when_fetched_at_missing(self) -> None:
        state = _build(
            _venue_snapshot(positions=[_venue_position()]),
            venue_fetched_at=None,
        )

        assessment = state.assess(ATOM_LONG)

        self.assertEqual(assessment.state, PositionState.UNKNOWN)
        self.assertFalse(assessment.venue_fresh)

    def test_conflicted_when_venue_flat_but_cache_open(self) -> None:
        state = _build(
            _venue_snapshot(positions=[]),
            cache_positions=[_cache_position(quantity="596.48")],
        )

        assessment = state.assess(ATOM_LONG)

        self.assertEqual(assessment.state, PositionState.CONFLICTED)
        self.assertEqual(assessment.quantity, Decimal("0"))
        self.assertTrue(assessment.venue_fresh)


class FreshnessWindowBoundaryTest(unittest.TestCase):
    def test_age_beyond_window_is_unknown(self) -> None:
        state = _build(
            _venue_snapshot(positions=[_venue_position()]),
            venue_fetched_at=NOW - timedelta(seconds=31),
        )

        assessment = state.assess(ATOM_LONG)

        self.assertEqual(assessment.state, PositionState.UNKNOWN)
        self.assertFalse(assessment.venue_fresh)

    def test_age_exactly_at_window_is_still_fresh(self) -> None:
        # 契约措辞为“超出 freshness_window → UNKNOWN”：等于窗口不算超出。
        state = _build(
            _venue_snapshot(positions=[_venue_position()]),
            venue_fetched_at=NOW - WINDOW,
        )

        assessment = state.assess(ATOM_LONG)

        self.assertEqual(assessment.state, PositionState.KNOWN_OPEN)
        self.assertTrue(assessment.venue_fresh)

    def test_age_just_beyond_window_is_unknown(self) -> None:
        state = _build(
            _venue_snapshot(positions=[_venue_position()]),
            venue_fetched_at=NOW - WINDOW - timedelta(microseconds=1),
        )

        self.assertEqual(state.assess(ATOM_LONG).state, PositionState.UNKNOWN)

    def test_custom_freshness_window_is_honoured(self) -> None:
        fetched_at = NOW - timedelta(seconds=45)
        snapshot = _venue_snapshot(positions=[_venue_position()])

        stale = _build(snapshot, venue_fetched_at=fetched_at)
        fresh = _build(
            snapshot,
            venue_fetched_at=fetched_at,
            freshness_window=timedelta(seconds=60),
        )

        self.assertEqual(stale.assess(ATOM_LONG).state, PositionState.UNKNOWN)
        self.assertEqual(fresh.assess(ATOM_LONG).state, PositionState.KNOWN_OPEN)


class PhantomPositionTest(unittest.TestCase):
    def test_phantom_cache_position_is_conflicted_not_flat(self) -> None:
        state = _build(
            _venue_snapshot(positions=[]),
            cache_positions=[
                _cache_position(quantity="5", position_id=f"{ATOM_INSTRUMENT}-LONG")
            ],
        )

        assessment = state.assess(ATOM_LONG)

        self.assertEqual(assessment.state, PositionState.CONFLICTED)
        self.assertNotEqual(assessment.state, PositionState.KNOWN_FLAT)

    def test_phantom_on_one_instrument_leaves_others_flat(self) -> None:
        other_book = BookKey(
            account_id=ACCOUNT_ID,
            instrument_id="BTCUSDT-PERP.BINANCE",
            position_side="LONG",
        )
        state = _build(
            _venue_snapshot(positions=[]),
            cache_positions=[_cache_position(quantity="5")],
        )

        self.assertEqual(state.assess(ATOM_LONG).state, PositionState.CONFLICTED)
        self.assertEqual(state.assess(other_book).state, PositionState.KNOWN_FLAT)


class CacheDriftTest(unittest.TestCase):
    def test_large_quantity_drift_stays_known_open_and_details_note_it(self) -> None:
        drifted = _build(
            _venue_snapshot(positions=[_venue_position(position_amt="100")]),
            cache_positions=[_cache_position(quantity="90")],
        )
        clean = _build(
            _venue_snapshot(positions=[_venue_position(position_amt="100")]),
            cache_positions=[_cache_position(quantity="100")],
        )

        drifted_assessment = drifted.assess(ATOM_LONG)
        clean_assessment = clean.assess(ATOM_LONG)

        self.assertEqual(drifted_assessment.state, PositionState.KNOWN_OPEN)
        self.assertEqual(drifted_assessment.quantity, Decimal("100"))
        self.assertTrue(drifted_assessment.detail)
        # >1% 漂移必须体现在 detail 里（与无漂移路径的判定依据可区分）。
        self.assertNotEqual(drifted_assessment.detail, clean_assessment.detail)

    def test_sub_percent_drift_is_treated_as_clean_known_open(self) -> None:
        state = _build(
            _venue_snapshot(positions=[_venue_position(position_amt="1000")]),
            cache_positions=[_cache_position(quantity="999.5")],
        )

        assessment = state.assess(ATOM_LONG)

        self.assertEqual(assessment.state, PositionState.KNOWN_OPEN)
        self.assertEqual(assessment.quantity, Decimal("1000"))


class BotOpenOrderClientIdsTest(unittest.TestCase):
    def test_returns_only_bot_orders_for_the_requested_book(self) -> None:
        bot_long = _bot_client_order_id(sequence=1)
        bot_short = _bot_client_order_id(sequence=2)
        state = _build(
            _venue_snapshot(
                positions=[_venue_position()],
                open_orders=[
                    _venue_order(position_side="LONG", client_order_id=bot_long),
                    _venue_order(position_side="LONG", client_order_id="web_manual_1"),
                    _venue_order(position_side="SHORT", client_order_id=bot_short),
                    _venue_order(
                        symbol="BTCUSDT",
                        position_side="LONG",
                        client_order_id=_bot_client_order_id(sequence=3),
                    ),
                ],
            ),
        )

        long_ids = state.bot_open_order_client_ids(ATOM_LONG)
        short_ids = state.bot_open_order_client_ids(ATOM_SHORT)

        self.assertIsInstance(long_ids, tuple)
        self.assertEqual(long_ids, (bot_long,))
        self.assertEqual(short_ids, (bot_short,))

    def test_bot_pattern_rejects_lookalike_client_ids(self) -> None:
        lookalikes = [
            "B" + "0" * 31 + "01",          # hex 段少一位
            "B" + "0" * 33 + "01",          # hex 段多一位
            "B" + "G" * 32 + "01",          # 非 hex 字符
            "b" + "0" * 32 + "01",          # 前缀小写
            "B" + "0" * 32 + "0a",          # 序号段非数字
            "B" + "0" * 32 + "011",         # 超长
        ]
        state = _build(
            _venue_snapshot(
                positions=[_venue_position()],
                open_orders=[
                    _venue_order(position_side="LONG", client_order_id=cid)
                    for cid in lookalikes
                ],
            ),
        )

        self.assertEqual(state.bot_open_order_client_ids(ATOM_LONG), ())

    def test_no_open_orders_yields_empty_tuple(self) -> None:
        state = _build(_venue_snapshot(positions=[_venue_position()]))

        self.assertEqual(state.bot_open_order_client_ids(ATOM_LONG), ())


class SemanticOperationIdTest(unittest.TestCase):
    ACTION = "replace_take_profits"

    def test_output_is_sha256_hex(self) -> None:
        op_id = semantic_operation_id(
            ACCOUNT_ID, "signal-123", self.ACTION, ATOM_LONG
        )

        self.assertIsInstance(op_id, str)
        self.assertRegex(op_id, r"^[0-9a-f]{64}$")

    def test_deterministic_for_identical_inputs(self) -> None:
        first = semantic_operation_id(
            ACCOUNT_ID, "signal-123", self.ACTION, ATOM_LONG, revision=3
        )
        second = semantic_operation_id(
            ACCOUNT_ID, "signal-123", self.ACTION, ATOM_LONG, revision=3
        )

        self.assertEqual(first, second)

    def test_derived_edit_suffixes_normalize_to_base_message(self) -> None:
        base = semantic_operation_id(
            ACCOUNT_ID, "signal-123", self.ACTION, ATOM_LONG
        )
        e1 = semantic_operation_id(
            ACCOUNT_ID, "signal-123-e1", self.ACTION, ATOM_LONG
        )
        e2 = semantic_operation_id(
            ACCOUNT_ID, "signal-123-e2", self.ACTION, ATOM_LONG
        )
        e12 = semantic_operation_id(
            ACCOUNT_ID, "signal-123-e12", self.ACTION, ATOM_LONG
        )

        self.assertEqual(e1, base)
        self.assertEqual(e2, base)
        self.assertEqual(e12, base)

    def test_non_edit_suffixes_are_not_stripped(self) -> None:
        base = semantic_operation_id(
            ACCOUNT_ID, "signal-123", self.ACTION, ATOM_LONG
        )
        trailing_word = semantic_operation_id(
            ACCOUNT_ID, "signal-123-extra", self.ACTION, ATOM_LONG
        )
        bare_e = semantic_operation_id(
            ACCOUNT_ID, "signal-123-e", self.ACTION, ATOM_LONG
        )

        self.assertNotEqual(trailing_word, base)
        self.assertNotEqual(bare_e, base)

    def test_revision_distinguishes_operations(self) -> None:
        rev0 = semantic_operation_id(
            ACCOUNT_ID, "signal-123", self.ACTION, ATOM_LONG, revision=0
        )
        rev1 = semantic_operation_id(
            ACCOUNT_ID, "signal-123", self.ACTION, ATOM_LONG, revision=1
        )
        default = semantic_operation_id(
            ACCOUNT_ID, "signal-123", self.ACTION, ATOM_LONG
        )

        self.assertNotEqual(rev0, rev1)
        self.assertEqual(default, rev0)

    def test_varies_by_account_action_and_book(self) -> None:
        base = semantic_operation_id(
            ACCOUNT_ID, "signal-123", self.ACTION, ATOM_LONG
        )
        other_account = semantic_operation_id(
            "account-b", "signal-123", self.ACTION, ATOM_LONG
        )
        other_action = semantic_operation_id(
            ACCOUNT_ID, "signal-123", "move_stop_loss", ATOM_LONG
        )
        other_side = semantic_operation_id(
            ACCOUNT_ID, "signal-123", self.ACTION, ATOM_SHORT
        )
        other_instrument = semantic_operation_id(
            ACCOUNT_ID,
            "signal-123",
            self.ACTION,
            BookKey(
                account_id=ACCOUNT_ID,
                instrument_id="BTCUSDT-PERP.BINANCE",
                position_side="LONG",
            ),
        )

        self.assertEqual(
            len({base, other_account, other_action, other_side, other_instrument}),
            5,
        )


class BotClientOrderIdPatternSanityTest(unittest.TestCase):
    """守住测试自身的 fixture：生成的 bot client id 必须匹配契约正则。"""

    def test_fixture_ids_match_contract_regex(self) -> None:
        pattern = re.compile(r"^B[0-9a-f]{32}[0-9]{2}$")
        for sequence in (0, 1, 99):
            self.assertRegex(_bot_client_order_id(sequence=sequence), pattern.pattern)


if __name__ == "__main__":
    unittest.main()
