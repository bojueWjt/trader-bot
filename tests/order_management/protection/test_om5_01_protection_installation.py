from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from psycopg2.extras import Json

from order_management.protection_manager import ProtectionInstallRequest, ProtectionManager
from strategy.intent_execution_planner import InstrumentSpec
from strategy.protection import PositionProtectionSnapshot, StopProtectionSpec, build_stop_order_plan


NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def test_stop_plan_uses_actual_position_quantity_and_native_stop_market() -> None:
    plan = build_stop_order_plan(
        intent_id="11111111-1111-4111-8111-111111111111",
        account_id="acct-om5-protect",
        instrument_id="BTCUSDT-PERP.BINANCE",
        position=PositionProtectionSnapshot(
            position_key="acct-om5-protect:BTCUSDT",
            side="long",
            quantity=Decimal("0.3754"),
            entry_price=Decimal("65000"),
        ),
        stop=StopProtectionSpec(stop_type="stop_market", trigger_price=Decimal("64000.114")),
        instrument=InstrumentSpec(
            instrument_id="BTCUSDT-PERP.BINANCE",
            price_increment="0.01",
            quantity_increment="0.001",
        ),
    )

    assert plan.order_type == "STOP_MARKET"
    assert plan.side == "SELL"
    assert plan.quantity == "0.375"
    assert plan.trigger_price == "64000.11"
    assert plan.reduce_only is True
    assert "lifecycle_role=stop_loss" in plan.tags
    assert "position_id=acct-om5-protect:BTCUSDT" in plan.tags


def test_install_after_entry_fill_records_active_stop_with_actual_position_quantity(db_conn) -> None:
    _seed_position(db_conn, account_id="acct-om5-protect", quantity=Decimal("0.375"))
    submitter = _RecordingSubmitter()
    manager = ProtectionManager(submitter=submitter)

    result = manager.install_after_entry_fill(
        db_conn,
        ProtectionInstallRequest(
            account_id="acct-om5-protect",
            position_key="acct-om5-protect:BTCUSDT",
            intent_id="11111111-1111-4111-8111-111111111111",
            stop=StopProtectionSpec(stop_type="stop_market", trigger_price=Decimal("64000")),
            instrument=InstrumentSpec(
                instrument_id="BTCUSDT-PERP.BINANCE",
                price_increment="0.01",
                quantity_increment="0.001",
            ),
            entry_filled_at=NOW,
            now=NOW,
            threshold_seconds=5,
        ),
    )

    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT lifecycle_role, status, active, quantity, trigger_price
            FROM protective_orders_projection
            WHERE account_id='acct-om5-protect' AND position_key='acct-om5-protect:BTCUSDT'
            """
        )
        row = cur.fetchone()

    assert result.status == "submitted"
    assert submitter.plans[0].quantity == "0.375"
    assert row == ("stop_loss", "working", True, Decimal("0.375"), Decimal("64000.00"))


def test_protection_submit_failure_halts_account_and_alerts(db_conn) -> None:
    _seed_position(db_conn, account_id="acct-om5-fail", quantity=Decimal("1"))
    alerts: list[dict] = []
    manager = ProtectionManager(
        submitter=_FailingSubmitter(),
        alert_sink=alerts.append,
        failure_mode="HALTED",
    )

    result = manager.install_after_entry_fill(
        db_conn,
        ProtectionInstallRequest(
            account_id="acct-om5-fail",
            position_key="acct-om5-fail:BTCUSDT",
            intent_id="22222222-2222-4222-8222-222222222222",
            stop=StopProtectionSpec(stop_type="stop_market", trigger_price=Decimal("64000")),
            instrument=InstrumentSpec(
                instrument_id="BTCUSDT-PERP.BINANCE",
                price_increment="0.01",
                quantity_increment="0.001",
            ),
            entry_filled_at=NOW,
            now=NOW,
            threshold_seconds=5,
        ),
    )

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT state->>'mode' FROM risk_state WHERE account_id='acct-om5-fail' AND instrument_id='BTCUSDT'"
        )
        mode = cur.fetchone()[0]

    assert result.status == "failed"
    assert result.risk_mode == "HALTED"
    assert mode == "HALTED"
    assert alerts[0]["type"] == "protection_install_failed"
    assert alerts[0]["position_key"] == "acct-om5-fail:BTCUSDT"


def _seed_position(conn, *, account_id: str, quantity: Decimal) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO positions_projection (
                account_id, position_id, instrument_id, side, quantity,
                avg_entry_price, status, updated_at, payload
            )
            VALUES (%s, %s, 'BTCUSDT-PERP.BINANCE', 'long', %s, 65000, 'open', %s, %s)
            """,
            (account_id, f"{account_id}:BTCUSDT", quantity, NOW, Json({})),
        )
    conn.commit()


class _RecordingSubmitter:
    def __init__(self) -> None:
        self.plans = []

    def submit_stop(self, plan):
        self.plans.append(plan)
        return {
            "client_order_id": plan.client_order_id,
            "venue_order_id": "venue-stop-1",
            "status": "working",
        }


class _FailingSubmitter:
    def submit_stop(self, plan):
        raise RuntimeError("exchange rejected stop")

