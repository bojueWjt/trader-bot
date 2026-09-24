from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "hermes-profile" / "scripts" / "report_data.py"
sys.path.insert(0, str(SCRIPT.parent))
import report_data  # noqa: E402

NOW = datetime(2026, 9, 23, 18, 0, tzinfo=timezone.utc)
CID_PROTECT = "B" + "ab" * 16 + "01"
CID_ENTRY = "B" + "cd" * 16 + "01"


def _hb_line(
    account: str,
    positions: list[dict],
    ts: str = "2026-09-23 18:00:00+00",
    *,
    snap_at: str | None = None,
    last_seen: str | None = None,
) -> str:
    payload = json.dumps({"reconciliation_state": "healthy"})
    seen = ts if last_seen is None else last_seen
    snap = ts if snap_at is None else snap_at
    return f"{account}|{seen}|{snap}|{json.dumps(positions)}|{payload}"


def test_completed_utc_window_matches_collector():
    sys.path.insert(0, str(REPO_ROOT / "services" / "control-plane" / "tools"))
    import exchange_income_collector as collector

    start, end = report_data.completed_utc_window(1, NOW)
    c_start, c_end = collector.completed_utc_window(1, NOW)
    assert (start, end) == (c_start, c_end)
    assert end == datetime(2026, 9, 23, tzinfo=timezone.utc)
    assert start == datetime(2026, 9, 22, tzinfo=timezone.utc)


def test_report_data_is_importable_and_marks_missing_income_nonzero():
    queries = {
        "node_heartbeats": [
            _hb_line("account-a", [{"symbol": "HYPEUSDT", "quantity": "27.00", "position_side": "LONG"}]),
            _hb_line("account-b", [{"symbol": "BTCUSDT", "quantity": "0.015", "position_side": "LONG"}]),
            _hb_line("account-c", [{"symbol": "SUIUSDT", "quantity": "1937.0", "position_side": "LONG"}]),
            _hb_line("account-d", [{"symbol": "SOLUSDT", "quantity": "0.25", "position_side": "LONG"}]),
        ],
        "fills": [
            "account-d|OrderFilled|8112627135|" + CID_PROTECT + "|BTCUSDT-PERP.BINANCE|SELL|0.235|83600|163.48640000|9.82300000|09-23 16:02:09|exchange_reconciliation|USDT|e-d-btc"
        ],
        "coverage": ["(查询失败: relation exchange_income_coverage does not exist)"],
        "income": ["(查询失败: relation exchange_income does not exist)"],
        "recon": [
            "account-d|2026-09-23 16:02:09|BTCUSDT-PERP.BINANCE|163.48640000|9.82300000|" + CID_PROTECT
        ],
        "equity": ["account-a|1000|800", "account-b|1|1", "account-c|1|1", "account-d|500|400"],
        "mirror": [
            "account-d|"
            + json.dumps(
                {
                    "positions": [
                        {"symbol": "SOLUSDT", "position_amt": "0.25", "position_side": "LONG"}
                    ],
                    "open_orders": [
                        {
                            "client_order_id": CID_PROTECT,
                            "symbol": "SOLUSDT",
                            "side": "SELL",
                            "reduce_only": True,
                            "position_side": "LONG",
                        },
                        {
                            "client_order_id": CID_ENTRY,
                            "symbol": "SOLUSDT",
                            "side": "BUY",
                            "reduce_only": False,
                        },
                    ],
                    "algo_orders": [],
                }
            )
            + "|2026-09-23 18:00:00+00|2026-09-23 18:00:00+00"
        ],
        "orders": [
            f"account-d|{CID_PROTECT}|SOLUSDT-PERP.BINANCE|short|true|accepted||LONG",
            f"account-d|{CID_ENTRY}|SOLUSDT-PERP.BINANCE|long|false|accepted|2026-09-01T00:00:00+00:00|LONG",
        ],
        "intents": ["09-23 10:00|BTCUSDT|open_position|approved|{}"],
        "rejected": ["OrderCanceled|2"],
        "open_orders": ["account-d|SOLUSDT-PERP.BINANCE|accepted|1"],
        "nodes": ["4"],
    }

    def query(sql: str) -> list[str]:
        if "lifecycle_role" in sql and "FROM trade_intents" in sql and "LEFT JOIN" not in sql:
            return queries.get("intent_plans") or []
        if "FROM trade_intents" in sql and "LEFT JOIN" not in sql:
            return queries["intents"]
        if "FROM node_heartbeats" in sql:
            return queries["node_heartbeats"]
        if "OrderPartiallyFilled" in sql:
            return queries["fills"]
        if "exchange_income_coverage" in sql:
            return queries["coverage"]
        if "FROM exchange_income" in sql:
            return queries["income"]
        if "FROM accounts_projection" in sql:
            return queries["equity"]
        if "FROM exchange_state_mirror" in sql:
            return queries["mirror"]
        if "FROM orders_projection o" in sql:
            return queries["orders"]
        if "GROUP BY 1,2,3" in sql:
            return queries["open_orders"]
        if "OrderRejected" in sql:
            return queries["rejected"]
        if "count(DISTINCT node_id)" in sql:
            return queries["nodes"]
        return []

    digest = report_data.render_digest(
        days=1,
        now=NOW,
        query=query,
        systemd_status={
            "trader-v3-controlplane-node-control": "active",
            "trader-v3-controlplane-operator-query": "active",
            "trader-v3-controlplane-event-ingest": "active",
        },
        window_start=datetime(2026, 9, 23, tzinfo=timezone.utc),
        window_end=NOW,
    )
    assert "数据缺失: exchange_income 未采集" in digest
    assert "163.48640000" in digest
    assert "补录观测，非 Binance 全量" in digest
    assert "HYPEUSDT LONG 27.00" in digest
    assert "SOLUSDT LONG 0.25" in digest
    assert "account-d 补录合计" in digest
    assert "qty=0.235" in digest
    assert "交易意向" in digest
    assert "被拒/异常事件计数" in digest
    assert "trader-v3-controlplane-node-control=active" in digest
    assert "无仓保护单:" in digest
    assert "仍在场" in digest
    assert "手动单不报警" in digest
    assert "REALIZED_PNL USDT=0" not in digest


def test_query_errors_are_data_missing_not_zero():
    def query(sql: str) -> list[str]:
        if "FROM node_heartbeats" in sql:
            return ["(查询失败: heartbeat down)"]
        if "OrderPartiallyFilled" in sql:
            return ["(查询失败: fills down)"]
        if "exchange_income_coverage" in sql:
            return ["account-a|2026-09-22 00:00:00+00|2026-09-23 00:00:00+00|t"]
        if "FROM exchange_income " in sql:
            return ["(查询失败: income down)"]
        if "FROM accounts_projection" in sql:
            return ["(查询失败: equity down)"]
        if "FROM exchange_state_mirror" in sql:
            return ["(查询失败: mirror down)"]
        if "FROM trade_intents" in sql:
            return ["(查询失败: intents down)"]
        return ["(查询失败: other)"]

    digest = report_data.render_digest(
        days=1,
        now=NOW,
        query=query,
        systemd_status={
            "trader-v3-controlplane-node-control": "active",
            "trader-v3-controlplane-operator-query": "active",
            "trader-v3-controlplane-event-ingest": "active",
        },
    )
    assert "数据缺失: heartbeat.positions" in digest
    assert "数据缺失: execution_events fills" in digest
    assert "account-a: 0" not in digest
    assert "数据缺失: exchange_reconciliation fills" in digest
    assert "(无非零补录 realized_pnl)" not in digest
    assert "数据缺失 权益" in digest
    assert "数据缺失: exchange_state_mirror" in digest
    assert "数据缺失: trade_intents" in digest


def test_income_attribution_and_closing_trade_count_not_lifecycle():
    def query(sql: str) -> list[str]:
        if "FROM node_heartbeats" in sql:
            return [_hb_line(account, []) for account in report_data.ACCOUNTS]
        if "OrderPartiallyFilled" in sql and "reconciliation" not in sql:
            return [
                "account-a|OrderFilled|t1|B" + "11" * 16 + "01|BTCUSDT-PERP.BINANCE|SELL|1|1|2|0.1|09-22 01:00:00|||e1"
            ]
        if "exchange_income_coverage" in sql:
            return [
                f"{account}|2026-09-22 00:00:00+00|2026-09-23 00:00:00+00|t"
                for account in report_data.ACCOUNTS
            ]
        if "FROM exchange_income " in sql:
            return [
                "account-a|REALIZED_PNL|USDT|2|t1|BTCUSDT",
                "account-a|COMMISSION|USDT|-0.1|t1|BTCUSDT",
                "account-a|FUNDING_FEE|USDT|0.5||",
            ]
        if "exchange_reconciliation" in sql:
            return []
        if "FROM accounts_projection" in sql:
            return [f"{account}|1|1" for account in report_data.ACCOUNTS]
        if "FROM exchange_state_mirror" in sql:
            return [
                f"{account}|" + json.dumps({"positions": [], "open_orders": [], "algo_orders": []}) + "|2026-09-23 18:00:00+00|2026-09-23 18:00:00+00"
                for account in report_data.ACCOUNTS
            ]
        if "FROM orders_projection o" in sql:
            return []
        if "FROM trade_intents" in sql:
            return []
        if "OrderRejected" in sql:
            return []
        if "count(DISTINCT node_id)" in sql:
            return ["4"]
        if "GROUP BY 1,2,3" in sql:
            return []
        return []

    digest = report_data.render_digest(
        days=1,
        now=NOW,
        query=query,
        systemd_status={
            "trader-v3-controlplane-node-control": "active",
            "trader-v3-controlplane-operator-query": "active",
            "trader-v3-controlplane-event-ingest": "active",
        },
    )
    assert "平仓成交笔数=1" in digest
    assert "不是完整仓位生命周期" in digest
    assert "attr[robot:USDT=2]" in digest
    assert "account" in digest and "FUNDING_FEE" in digest


def test_weekly_entrypoint_imports():
    weekly = REPO_ROOT / "hermes-profile" / "scripts" / "report_data_weekly.py"
    assert weekly.is_file()
    text = weekly.read_text(encoding="utf-8")
    assert "from report_data import main" in text


def _empty_mirrors():
    return [
        f"{account}|"
        + json.dumps({"positions": [], "open_orders": [], "algo_orders": []})
        + "|2026-09-23 18:00:00+00|2026-09-23 18:00:00+00"
        for account in report_data.ACCOUNTS
    ]


def _base_query(**overrides):
    data = {
        "hb": [_hb_line(account, []) for account in report_data.ACCOUNTS],
        "fills": [],
        "coverage": [],
        "income": ["(查询失败: missing)"],
        "equity": [f"{account}|1|1" for account in report_data.ACCOUNTS],
        "mirror": _empty_mirrors(),
        "orders": [],
        "intents": [],
        "intent_plans": [],
        "rejected": [],
        "open_orders": [],
        "nodes": ["4"],
    }
    data.update(overrides)

    def query(sql: str) -> list[str]:
        if "lifecycle_role" in sql and "FROM trade_intents" in sql and "LEFT JOIN" not in sql:
            return data["intent_plans"]
        if "FROM trade_intents" in sql and "LEFT JOIN" not in sql:
            return data["intents"]
        if "FROM node_heartbeats" in sql:
            return data["hb"]
        if "OrderPartiallyFilled" in sql:
            return data["fills"]
        if "exchange_income_coverage" in sql:
            return data["coverage"]
        if "FROM exchange_income" in sql:
            return data["income"]
        if "FROM accounts_projection" in sql:
            return data["equity"]
        if "FROM exchange_state_mirror" in sql:
            return data["mirror"]
        if "FROM orders_projection o" in sql:
            return data["orders"]
        if "GROUP BY 1,2,3" in sql:
            return data["open_orders"]
        if "OrderRejected" in sql:
            return data["rejected"]
        if "count(DISTINCT node_id)" in sql:
            return data["nodes"]
        return []

    return query


def test_fresh_snapshot_just_after_render_start_is_valid():
    later = "2026-09-23 18:00:02+00"
    digest = report_data.render_digest(
        days=1,
        now=NOW,
        query=_base_query(hb=[_hb_line(account, [], ts=later) for account in report_data.ACCOUNTS]),
        systemd_status={u: "数据缺失" for u in report_data.CONTROLPLANE_UNITS},
        window_start=datetime(2026, 9, 23, tzinfo=timezone.utc),
        window_end=NOW,
    )
    assert "不新鲜" not in digest
    assert "空仓" in digest


def test_stale_heartbeat_fresh_mirror_does_not_claim_books():
    stale = "2026-09-22 18:00:00+00"
    protect = {
        "client_order_id": CID_PROTECT,
        "symbol": "SOLUSDT",
        "side": "SELL",
        "reduce_only": True,
        "position_side": "LONG",
    }
    digest = report_data.render_digest(
        days=1,
        now=NOW,
        query=_base_query(
            hb=[_hb_line(account, [], ts=stale) for account in report_data.ACCOUNTS],
            mirror=[
                f"{account}|"
                + json.dumps(
                    {
                        "positions": [
                            {"symbol": "SOLUSDT", "position_amt": "0.25", "position_side": "LONG"}
                        ],
                        "open_orders": [protect],
                        "algo_orders": [],
                    }
                )
                + "|2026-09-23 18:00:00+00|2026-09-23 18:00:00+00"
                for account in report_data.ACCOUNTS
            ],
        ),
        systemd_status={u: "数据缺失" for u in report_data.CONTROLPLANE_UNITS},
        window_start=datetime(2026, 9, 23, tzinfo=timezone.utc),
        window_end=NOW,
    )
    assert "数据缺失 heartbeat" in digest or "不新鲜" in digest
    assert "无对应仓" not in digest


def test_empty_healthy_heartbeat_does_not_fallback_to_mirror():
    protect = {
        "client_order_id": CID_PROTECT,
        "symbol": "SOLUSDT",
        "side": "SELL",
        "reduce_only": True,
        "position_side": "LONG",
    }
    digest = report_data.render_digest(
        days=1,
        now=NOW,
        query=_base_query(
            hb=[_hb_line(account, []) for account in report_data.ACCOUNTS],
            mirror=[
                f"{account}|"
                + json.dumps(
                    {
                        "positions": [
                            {"symbol": "SOLUSDT", "position_amt": "0.25", "position_side": "LONG"}
                        ],
                        "open_orders": [protect],
                        "algo_orders": [],
                    }
                )
                + "|2026-09-23 18:00:00+00|2026-09-23 18:00:00+00"
                for account in report_data.ACCOUNTS
            ],
        ),
        systemd_status={u: "数据缺失" for u in report_data.CONTROLPLANE_UNITS},
        window_start=datetime(2026, 9, 23, tzinfo=timezone.utc),
        window_end=NOW,
    )
    assert "无对应仓" in digest
    assert "空仓" in digest


def test_stop_entry_is_expired_not_orphan_and_reduce_only_is_orphan():
    cid_stop_entry = "B" + "11" * 16 + "01"
    cid_sl = "B" + "22" * 16 + "01"
    digest = report_data.render_digest(
        days=1,
        now=NOW,
        query=_base_query(
            hb=[_hb_line(account, []) for account in report_data.ACCOUNTS],
            mirror=[
                (
                    f"{account}|"
                    + json.dumps(
                        {
                            "positions": [],
                            "open_orders": [
                                {
                                    "client_order_id": cid_stop_entry,
                                    "symbol": "BTCUSDT",
                                    "side": "BUY",
                                    "reduce_only": False,
                                    "type": "STOP_MARKET",
                                },
                                {
                                    "client_order_id": cid_sl,
                                    "symbol": "ETHUSDT",
                                    "side": "SELL",
                                    "reduce_only": True,
                                    "type": "STOP_MARKET",
                                    "position_side": "LONG",
                                },
                            ],
                            "algo_orders": [],
                        }
                    )
                    + "|2026-09-23 18:00:00+00|2026-09-23 18:00:00+00"
                )
                if account == "account-a"
                else f"{account}|"
                + json.dumps({"positions": [], "open_orders": [], "algo_orders": []})
                + "|2026-09-23 18:00:00+00|2026-09-23 18:00:00+00"
                for account in report_data.ACCOUNTS
            ],
            orders=[
                f"account-a|{cid_stop_entry}|BTCUSDT-PERP.BINANCE|long|false|accepted|2026-09-01T00:00:00+00:00|LONG|entry",
                f"account-a|{cid_sl}|ETHUSDT-PERP.BINANCE|short|true|accepted||LONG|stop_loss",
            ],
        ),
        systemd_status={u: "数据缺失" for u in report_data.CONTROLPLANE_UNITS},
        window_start=datetime(2026, 9, 23, tzinfo=timezone.utc),
        window_end=NOW,
    )
    assert cid_stop_entry in digest and "仍在场" in digest
    assert cid_sl in digest and "无对应仓" in digest


def test_cid_without_projection_uses_intent_or_missing_expiry():
    digest = report_data.render_digest(
        days=1,
        now=NOW,
        query=_base_query(
            hb=[_hb_line(account, []) for account in report_data.ACCOUNTS],
            mirror=[
                (
                    f"{account}|"
                    + json.dumps(
                        {
                            "positions": [],
                            "open_orders": [
                                {
                                    "client_order_id": CID_ENTRY,
                                    "symbol": "SOLUSDT",
                                    "side": "BUY",
                                    "reduce_only": False,
                                }
                            ],
                            "algo_orders": [],
                        }
                    )
                    + "|2026-09-23 18:00:00+00|2026-09-23 18:00:00+00"
                )
                if account == "account-d"
                else f"{account}|"
                + json.dumps({"positions": [], "open_orders": [], "algo_orders": []})
                + "|2026-09-23 18:00:00+00|2026-09-23 18:00:00+00"
                for account in report_data.ACCOUNTS
            ],
            orders=[],
            intent_plans=[],
        ),
        systemd_status={u: "数据缺失" for u in report_data.CONTROLPLANE_UNITS},
        window_start=datetime(2026, 9, 23, tzinfo=timezone.utc),
        window_end=NOW,
    )
    assert "数据缺失: entry expiry metadata" in digest
    assert "(无)" not in digest.split("过期仍在场 entry:")[1].split("无仓保护单:")[0]


def test_income_attr_includes_asset_and_conflict_before_dedup():
    robot = "B" + "11" * 16 + "01"
    manual = "aos_manual"
    digest = report_data.render_digest(
        days=1,
        now=NOW,
        query=_base_query(
            coverage=[
                f"{account}|2026-09-23 00:00:00+00|2026-09-23 18:00:00+00|t"
                for account in report_data.ACCOUNTS
            ],
            income=[
                "account-a|REALIZED_PNL|USDT|2|t1|BTCUSDT",
                "account-a|COMMISSION|BNB|-0.01|t1|BTCUSDT",
            ],
            fills=[
                f"account-a|OrderFilled|t1|{robot}|BTCUSDT-PERP.BINANCE|SELL|1|1|2||09-23 01:00:00|||e1",
                f"account-a|OrderFilled|t1|{manual}|BTCUSDT-PERP.BINANCE|SELL|1|1|||09-23 01:00:01|||e2",
            ],
        ),
        systemd_status={u: "数据缺失" for u in report_data.CONTROLPLANE_UNITS},
        window_start=datetime(2026, 9, 23, tzinfo=timezone.utc),
        window_end=NOW,
    )
    assert "attr[unknown:USDT=2]" in digest
    assert "BNB=-0.01" in digest
    assert "unknown:BNB=-0.01" in digest


def test_malformed_income_is_missing_not_zero():
    digest = report_data.render_digest(
        days=1,
        now=NOW,
        query=_base_query(
            coverage=[
                f"{account}|2026-09-23 00:00:00+00|2026-09-23 18:00:00+00|t"
                for account in report_data.ACCOUNTS
            ],
            income=["account-a|REALIZED_PNL|USDT|not-a-number|t1|BTCUSDT"],
            fills=["account-a|OrderFilled|t1|B" + "11" * 16 + "01|BTCUSDT-PERP.BINANCE|SELL|1|1|2|0.1|09-23 01:00:00|||e1"],
        ),
        systemd_status={u: "数据缺失" for u in report_data.CONTROLPLANE_UNITS},
        window_start=datetime(2026, 9, 23, tzinfo=timezone.utc),
        window_end=NOW,
    )
    assert "数据缺失 income 行损坏" in digest
    assert "REALIZED_PNL USDT=0" not in digest


def test_recon_fees_include_zero_pnl_and_missing_fee():
    digest = report_data.render_digest(
        days=1,
        now=NOW,
        query=_base_query(
            fills=[
                "account-d|OrderFilled|8112627135|" + CID_PROTECT + "|BTCUSDT-PERP.BINANCE|SELL|0.235|83600|163.48640000|9.82300000|09-23 16:02:09|exchange_reconciliation|USDT|e1",
                "account-d|OrderFilled|entry1|" + CID_ENTRY + "|BTCUSDT-PERP.BINANCE|BUY|0.087|85000|0|1.1|09-23 14:10:46|exchange_reconciliation|USDT|e2",
                "account-d|OrderFilled|entry2|aos_x|ETHUSDT-PERP.BINANCE|BUY|1|1|0||09-23 14:11:00|exchange_reconciliation|USDT|e3",
            ]
        ),
        systemd_status={u: "数据缺失" for u in report_data.CONTROLPLANE_UNITS},
        window_start=datetime(2026, 9, 23, tzinfo=timezone.utc),
        window_end=NOW,
    )
    assert "pnl=0" in digest
    assert "commission=1.1" in digest
    assert "commission=数据缺失" in digest
    assert "attr=robot" in digest
    assert "attr=manual" in digest


def test_q_timeout_is_data_missing(monkeypatch):
    def boom(*_args, **_kwargs):
        raise TimeoutError("expired")

    import subprocess

    def raise_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="psql", timeout=20)

    monkeypatch.setattr(subprocess, "run", raise_timeout)
    rows = report_data.q("SELECT 1")
    assert rows and rows[0].startswith("(查询失败:")


def _status():
    return {unit: "数据缺失" for unit in report_data.CONTROLPLANE_UNITS}


def test_bad_quantity_rejects_account_from_hb_valid():
    digest = report_data.render_digest(
        days=1,
        now=NOW,
        query=_base_query(
            hb=[
                _hb_line("account-a", []),
                _hb_line("account-b", []),
                _hb_line("account-c", []),
                _hb_line(
                    "account-d",
                    [{"symbol": "SOLUSDT", "quantity": "inf", "position_side": "LONG"}],
                ),
            ],
            mirror=[
                (
                    f"{account}|"
                    + json.dumps(
                        {
                            "positions": [],
                            "open_orders": (
                                [
                                    {
                                        "client_order_id": CID_PROTECT,
                                        "symbol": "SOLUSDT",
                                        "side": "SELL",
                                        "reduce_only": True,
                                        "position_side": "LONG",
                                    }
                                ]
                                if account == "account-d"
                                else []
                            ),
                            "algo_orders": [],
                        }
                    )
                    + "|2026-09-23 18:00:00+00|2026-09-23 18:00:00+00"
                )
                for account in report_data.ACCOUNTS
            ],
        ),
        systemd_status=_status(),
        window_start=datetime(2026, 9, 23, tzinfo=timezone.utc),
        window_end=NOW,
    )
    assert "account-d: 数据缺失 positions 行损坏" in digest
    assert "SOLUSDT LONG" not in digest
    assert "无对应仓" not in digest


def test_bad_side_rejects_account_from_hb_valid():
    digest = report_data.render_digest(
        days=1,
        now=NOW,
        query=_base_query(
            hb=[
                _hb_line(account, [])
                if account != "account-a"
                else _hb_line(
                    "account-a",
                    [{"symbol": "HYPEUSDT", "quantity": "27", "position_side": "WEIRD"}],
                )
                for account in report_data.ACCOUNTS
            ]
        ),
        systemd_status=_status(),
        window_start=datetime(2026, 9, 23, tzinfo=timezone.utc),
        window_end=NOW,
    )
    assert "account-a: 数据缺失 positions 行损坏" in digest
    assert "HYPEUSDT" not in digest.split("### 成交")[0]


def test_missing_snapshot_at_does_not_fallback_to_last_seen():
    digest = report_data.render_digest(
        days=1,
        now=NOW,
        query=_base_query(
            hb=[
                _hb_line(
                    account,
                    [{"symbol": "BTCUSDT", "quantity": "1", "position_side": "LONG"}],
                    snap_at="",
                    last_seen="2026-09-23 18:00:00+00",
                )
                for account in report_data.ACCOUNTS
            ]
        ),
        systemd_status=_status(),
        window_start=datetime(2026, 9, 23, tzinfo=timezone.utc),
        window_end=NOW,
    )
    assert "不新鲜或不健康" in digest
    assert "BTCUSDT LONG" not in digest.split("### 成交")[0]


def test_malformed_order_does_not_keep_prior_candidates():
    digest = report_data.render_digest(
        days=1,
        now=NOW,
        query=_base_query(
            hb=[_hb_line(account, []) for account in report_data.ACCOUNTS],
            mirror=[
                (
                    "account-a|"
                    + json.dumps(
                        {
                            "positions": [],
                            "open_orders": [
                                {
                                    "client_order_id": CID_PROTECT,
                                    "symbol": "SOLUSDT",
                                    "side": "SELL",
                                    "reduce_only": True,
                                    "position_side": "LONG",
                                },
                                "not-an-order",
                            ],
                            "algo_orders": [],
                        }
                    )
                    + "|2026-09-23 18:00:00+00|2026-09-23 18:00:00+00"
                )
            ]
            + [
                f"{account}|"
                + json.dumps({"positions": [], "open_orders": [], "algo_orders": []})
                + "|2026-09-23 18:00:00+00|2026-09-23 18:00:00+00"
                for account in report_data.ACCOUNTS
                if account != "account-a"
            ],
        ),
        systemd_status=_status(),
        window_start=datetime(2026, 9, 23, tzinfo=timezone.utc),
        window_end=NOW,
    )
    assert "无对应仓" not in digest
    assert "account-a" in digest and "数据缺失" in digest


def test_robot_order_missing_reduce_is_data_missing_not_guessed():
    digest = report_data.render_digest(
        days=1,
        now=NOW,
        query=_base_query(
            hb=[_hb_line(account, []) for account in report_data.ACCOUNTS],
            mirror=[
                (
                    "account-d|"
                    + json.dumps(
                        {
                            "positions": [],
                            "open_orders": [
                                {
                                    "client_order_id": CID_ENTRY,
                                    "symbol": "SOLUSDT",
                                    "side": "BUY",
                                }
                            ],
                            "algo_orders": [],
                        }
                    )
                    + "|2026-09-23 18:00:00+00|2026-09-23 18:00:00+00"
                )
            ]
            + [
                f"{account}|"
                + json.dumps({"positions": [], "open_orders": [], "algo_orders": []})
                + "|2026-09-23 18:00:00+00|2026-09-23 18:00:00+00"
                for account in report_data.ACCOUNTS
                if account != "account-d"
            ],
        ),
        systemd_status=_status(),
        window_start=datetime(2026, 9, 23, tzinfo=timezone.utc),
        window_end=NOW,
    )
    assert CID_ENTRY not in digest
    assert "无对应仓" not in digest
    assert "数据缺失" in digest


def test_manual_cid_still_skipped():
    digest = report_data.render_digest(
        days=1,
        now=NOW,
        query=_base_query(
            hb=[_hb_line(account, []) for account in report_data.ACCOUNTS],
            mirror=[
                f"{account}|"
                + json.dumps(
                    {
                        "positions": [],
                        "open_orders": [
                            {
                                "client_order_id": "aos_manual",
                                "symbol": "SOLUSDT",
                                "side": "SELL",
                                "reduce_only": True,
                                "position_side": "LONG",
                            }
                        ],
                        "algo_orders": [],
                    }
                )
                + "|2026-09-23 18:00:00+00|2026-09-23 18:00:00+00"
                for account in report_data.ACCOUNTS
            ],
        ),
        systemd_status=_status(),
        window_start=datetime(2026, 9, 23, tzinfo=timezone.utc),
        window_end=NOW,
    )
    assert "aos_manual" not in digest
    assert "无对应仓" not in digest


def test_single_income_type_coverage_is_not_complete():
    digest = report_data.render_digest(
        days=1,
        now=NOW,
        query=_base_query(
            coverage=[
                f"{account}|2026-09-23 00:00:00+00|2026-09-23 18:00:00+00|t|REALIZED_PNL"
                for account in report_data.ACCOUNTS
            ],
            income=["account-a|REALIZED_PNL|USDT|2|t1|BTCUSDT"],
        ),
        systemd_status=_status(),
        window_start=datetime(2026, 9, 23, tzinfo=timezone.utc),
        window_end=NOW,
    )
    assert "无完整 income coverage" in digest
    assert "attr[robot:USDT=2]" not in digest


def test_invalid_days_or_empty_window_is_rejected():
    empty = report_data.render_digest(
        days=0,
        now=NOW,
        query=_base_query(),
        systemd_status=_status(),
    )
    assert "数据缺失 无效窗口" in empty
    assert "REALIZED_PNL" not in empty
    same = report_data.render_digest(
        days=1,
        now=NOW,
        query=_base_query(),
        systemd_status=_status(),
        window_start=NOW,
        window_end=NOW,
    )
    assert "数据缺失 无效窗口" in same
