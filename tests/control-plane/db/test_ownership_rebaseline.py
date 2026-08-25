from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest
from psycopg2.extras import Json

REPO_ROOT = Path(__file__).resolve().parents[3]
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
CONTROL_PLANE_ROOT = REPO_ROOT / "services" / "control-plane"
SCRIPT_PATH = REPO_ROOT / "scripts" / "ownership_ledger.py"
for module_path in (EXECUTION_DOMAIN_ROOT, CONTROL_PLANE_ROOT):
    if str(module_path) not in sys.path:
        sys.path.insert(0, str(module_path))

from execution_domain.ownership_ledger import (
    OwnershipLedgerError,
    build_rebaseline_payload,
    load_robot_owned_balance,
)
from order_management.protection_watchdog import ProtectionWatchdog


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "ownership_ledger_script",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_marker_resets_history_and_keeps_later_fills(
    migrated_db: str,
) -> None:
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        _insert_fill(cur, side=1, quantity="2")
        _insert_marker(cur, baseline_quantity="0")
        _insert_fill(cur, side=2, quantity="0.25")

        rebased = load_robot_owned_balance(
            cur,
            account_id="account-a",
            symbol="BTCUSDT",
        )
        raw = load_robot_owned_balance(
            cur,
            account_id="account-a",
            symbol="BTCUSDT",
            apply_rebaseline=False,
        )

    assert rebased.baseline_quantity == Decimal(0)
    assert rebased.post_baseline_fill_quantity == Decimal("-0.25")
    assert rebased.quantity == Decimal("-0.25")
    assert raw.quantity == Decimal("1.75")


def test_rebaseline_payload_requires_exchange_decomposition() -> None:
    with pytest.raises(
        OwnershipLedgerError,
        match="exchange quantity must equal",
    ):
        build_rebaseline_payload(
            symbol="BTCUSDT",
            baseline_quantity=Decimal(0),
            reason="user adjudication",
            adjudicated_by="user",
            request_id="ownership-test",
            exchange_quantity=Decimal(1),
            manual_quantity=Decimal("0.5"),
            previous_robot_owned_quantity=Decimal(2),
            raw_robot_fill_quantity=Decimal(2),
        )


def test_record_rebaseline_writes_typed_marker_and_audit(
    migrated_db: str,
) -> None:
    module = _load_script()
    with psycopg2.connect(migrated_db) as conn:
        _seed_active_heartbeat(conn)
        with conn.cursor() as cur:
            _insert_fill(cur, side=1, quantity="2")
        conn.commit()

        result = module.record_rebaseline(
            conn,
            account_id="account-a",
            symbol="BTCUSDT",
            baseline_quantity=Decimal(0),
            reason="user-adjudicated manual domain 2026-08-24",
            adjudicated_by="user",
            request_id="ownership-test-account-a-btc",
            expected_exchange_quantity=Decimal("1.5"),
            manual_quantity=Decimal("1.5"),
        )

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT event_type, client_order_id, payload
                FROM execution_events
                WHERE event_id=%s
                """,
                (result["event_id"],),
            )
            event_type, client_order_id, payload = cur.fetchone()
            cur.execute(
                """
                SELECT event_type, reason, payload
                FROM audit_events
                WHERE aggregate_id='account-a:BTCUSDT'
                  AND event_type='OwnershipRebaseline'
                """
            )
            audit_event_type, reason, audit_payload = cur.fetchone()

    assert result["balance"]["robot_owned_quantity"] == "0"
    assert event_type == "OwnershipRebaseline"
    assert client_order_id is None
    assert payload["baseline_quantity"] == "0"
    assert audit_event_type == "OwnershipRebaseline"
    assert reason == "user-adjudicated manual domain 2026-08-24"
    assert (
        audit_payload["execution_event_id"]
        == result["event_id"]
    )


def test_watchdog_uses_rebased_robot_owned_balance(
    migrated_db: str,
) -> None:
    with psycopg2.connect(migrated_db) as conn:
        _seed_watchdog_position(conn)
        with conn.cursor() as cur:
            _insert_fill(cur, side=1, quantity="1")
        conn.commit()
        watchdog = ProtectionWatchdog()

        before = watchdog.check(
            conn,
            account_id="account-a",
            venue_working_orders=[],
            policy="repair",
            now=datetime.now(timezone.utc),
        )

        with conn.cursor() as cur:
            _insert_marker(cur, baseline_quantity="0")
        conn.commit()
        after = watchdog.check(
            conn,
            account_id="account-a",
            venue_working_orders=[],
            policy="repair",
            now=datetime.now(timezone.utc),
        )

    assert before.missing_count == 1
    assert after.missing_count == 0


def test_audit_manual_attribution_starts_at_latest_baseline(
    migrated_db: str,
) -> None:
    module = _load_script()
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        _insert_manual_fill(cur, side=1, quantity="0.4")
        _insert_marker(cur, baseline_quantity="0")
        _insert_manual_fill(cur, side=2, quantity="0.5")
        baselines = module.load_latest_ownership_baselines(
            cur,
            account_id="account-a",
        )
        quantities, counts = module._manual_fill_balances(
            cur,
            account_id="account-a",
            baselines=baselines,
        )

    assert quantities["BTCUSDT"] == Decimal("1.0")
    assert counts["BTCUSDT"] == 1


def _insert_fill(cur, *, side: int, quantity: str) -> None:
    cur.execute(
        """
        INSERT INTO execution_events (
            execution_event_row_id,
            event_id,
            node_id,
            account_id,
            client_order_id,
            event_type,
            ts_event,
            payload
        )
        VALUES (%s, %s, 'node-a', 'account-a', %s,
                'OrderFilled', clock_timestamp(), %s)
        """,
        (
            str(uuid4()),
            str(uuid4()),
            "B" + uuid4().hex + "01",
            Json(
                {
                    "instrument_id": "BTCUSDT-PERP.BINANCE",
                    "order_side": side,
                    "last_qty": quantity,
                }
            ),
        ),
    )


def _insert_manual_fill(cur, *, side: int, quantity: str) -> None:
    cur.execute(
        """
        INSERT INTO execution_events (
            execution_event_row_id,
            event_id,
            node_id,
            account_id,
            client_order_id,
            event_type,
            ts_event,
            payload
        )
        VALUES (%s, %s, 'node-a', 'account-a', %s,
                'OrderFilled', clock_timestamp(), %s)
        """,
        (
            str(uuid4()),
            str(uuid4()),
            f"aos_{uuid4().hex}",
            Json(
                {
                    "instrument_id": "BTCUSDT-PERP.BINANCE",
                    "order_side": side,
                    "last_qty": quantity,
                }
            ),
        ),
    )


def _insert_marker(cur, *, baseline_quantity: str) -> None:
    cur.execute(
        """
        INSERT INTO execution_events (
            execution_event_row_id,
            event_id,
            node_id,
            account_id,
            event_type,
            ts_event,
            payload
        )
        VALUES (%s, %s, 'operator-ownership-ledger', 'account-a',
                'OwnershipRebaseline', clock_timestamp(), %s)
        """,
        (
            str(uuid4()),
            f"ownership-rebaseline:{uuid4()}",
            Json(
                {
                    "ownership_schema_version": (
                        "ownership-rebaseline/v1"
                    ),
                    "symbol": "BTCUSDT",
                    "baseline_quantity": baseline_quantity,
                    "exchange_quantity_at_baseline": "1.5",
                    "manual_quantity_at_baseline": "1.5",
                    "reason": "test manual domain",
                    "adjudicated_by": "test-user",
                    "request_id": str(uuid4()),
                }
            ),
        ),
    )


def _seed_active_heartbeat(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO node_heartbeats (
                node_id,
                account_id,
                status,
                capabilities,
                payload,
                last_seen_at,
                positions,
                regular_orders,
                algo_orders,
                positions_snapshot_at,
                regular_orders_snapshot_at,
                algo_orders_snapshot_at
            )
            VALUES (
                'node-a',
                'account-a',
                'ACTIVE',
                '[]',
                '{}',
                clock_timestamp(),
                %s,
                '[]',
                %s,
                clock_timestamp(),
                clock_timestamp(),
                clock_timestamp()
            )
            """,
            (
                Json(
                    [
                        {
                            "symbol": "BTCUSDT",
                            "quantity": "1.5",
                            "position_side": "LONG",
                        }
                    ]
                ),
                Json(
                    [
                        {
                            "symbol": "BTCUSDT",
                            "client_order_id": "stToAg_manual_stop",
                        }
                    ]
                ),
            ),
        )
    conn.commit()


def _seed_watchdog_position(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO positions_projection (
                account_id,
                position_id,
                instrument_id,
                side,
                quantity,
                status,
                updated_at,
                payload
            )
            VALUES (
                'account-a',
                'account-a:BTCUSDT',
                'BTCUSDT',
                'long',
                1,
                'open',
                clock_timestamp(),
                '{}'
            )
            """
        )
    conn.commit()
