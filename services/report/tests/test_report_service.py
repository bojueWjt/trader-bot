import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("report_service", ROOT / "report_service.py")
report_service = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report_service)


def base_payload():
    return {
        "type": "daily",
        "date": "2026-07-12",
        "title": "Daily",
        "sections": {"overview_md": "Intro **risk**\n\n- one\n- <two>"},
        "channel_views": [
            {
                "channel": "C02",
                "trader": "舒琴",
                "stance": "mixed",
                "summary": "BTC range-bound",
                "symbols": ["BTCUSDT"],
            }
        ],
        "images": [],
        "extra_metrics": [],
    }


class FakeCursor:
    def __init__(self, responses=None, failures=None):
        self.responses = responses or {}
        self.failures = failures or {}
        self.executions = []
        self.current_rows = []

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return False

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        key = self.query_key(normalized)
        self.executions.append((normalized, params))
        failure = self.failures.get(key)
        if failure:
            raise RuntimeError(failure)
        self.current_rows = self.responses.get(key, [])

    def fetchall(self):
        return self.current_rows

    @staticmethod
    def query_key(sql):
        if "FROM trade_outcome_job_runs" in sql:
            return "watermark"
        if "FROM trade_outcomes" in sql:
            return "trade_outcomes"
        if "FROM execution_events" in sql:
            return "window_fills"
        if "FROM trade_intents" in sql:
            return "trade_intents"
        if "FROM exchange_state_mirror" in sql:
            return "exchange_state_mirror"
        if "FROM accounts_projection" in sql:
            return "accounts_projection"
        if "FROM positions_projection" in sql:
            return "positions_projection"
        raise AssertionError(f"unexpected query: {sql}")


class FakeConnection:
    def __init__(self, cursor):
        self.autocommit = False
        self.fake_cursor = cursor
        self.closed = False

    def cursor(self, cursor_factory=None):
        assert cursor_factory is report_service.RealDictCursor
        return self.fake_cursor

    def close(self):
        self.closed = True


def install_fake_database(monkeypatch, responses=None, failures=None):
    cursor = FakeCursor(responses=responses, failures=failures)
    connection = FakeConnection(cursor)

    class FakePsycopg:
        @staticmethod
        def connect(dsn):
            assert dsn == "postgres://example"
            return connection

    monkeypatch.setattr(report_service, "psycopg2", FakePsycopg())
    monkeypatch.setattr(report_service, "RealDictCursor", object())
    return cursor, connection


def app_endpoint(app, path, method):
    for route in app.routes:
        methods = route.methods or set()
        if route.path == path and method in methods:
            return route.endpoint
    raise AssertionError(f"missing route: {method} {path}")


def healthy_dependencies():
    dependencies = report_service.empty_dependency_status()
    dependencies["database"] = {"status": "ok", "reason": ""}
    dependencies["trade_outcomes"].update(
        {
            "status": "ok",
            "reason": "",
            "completed_at": "2026-07-29T00:00:00+00:00",
            "age_seconds": 3600,
            "freshness_threshold_seconds": 129600,
        }
    )
    dependencies["exchange_state_mirror"] = {"status": "ok", "reason": ""}
    return dependencies


def test_render_markdown_supports_paragraphs_bold_and_lists():
    html = report_service.render_markdown("Intro **risk**\n\n- one\n- <two>")

    assert "<p>Intro <strong>risk</strong></p>" in html
    assert "<ul><li>one</li><li>&lt;two&gt;</li></ul>" in html


def test_validate_payload_requires_known_type_and_valid_channel_views():
    with pytest.raises(report_service.ReportValidationError):
        report_service.normalize_payload({"type": "monthly"})

    with pytest.raises(report_service.ReportValidationError):
        report_service.normalize_payload({"type": "daily", "channel_views": [{"channel": "c"}]})

    payload = report_service.normalize_payload(base_payload())
    assert payload["type"] == "daily"
    assert payload["date"] == "2026-07-12"


def test_fetch_report_data_fails_closed_when_psycopg2_connect_fails(monkeypatch):
    class BrokenPsycopg:
        @staticmethod
        def connect(_dsn):
            raise RuntimeError("database offline")

    monkeypatch.setattr(report_service, "psycopg2", BrokenPsycopg())
    dependencies = report_service.empty_dependency_status()

    with pytest.raises(report_service.ReportDependencyError, match="database offline"):
        report_service.fetch_report_data(
            "daily",
            "2026-07-12",
            "postgres://example",
            dependency_status=dependencies,
        )

    assert dependencies["database"]["status"] == "error"


def test_fetch_report_data_requires_fixed_success_watermark(monkeypatch):
    now = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
    responses = {
        "watermark": [{"completed_at": now - timedelta(hours=1)}],
    }
    cursor, connection = install_fake_database(monkeypatch, responses=responses)
    dependencies = report_service.empty_dependency_status()

    data = report_service.fetch_report_data(
        "daily",
        "2026-07-12",
        "postgres://example",
        dependency_status=dependencies,
        now=now,
    )

    watermark_queries = [
        execution
        for execution in cursor.executions
        if "FROM trade_outcome_job_runs" in execution[0]
    ]
    assert len(watermark_queries) == 1
    assert watermark_queries[0][1] == ("trade_outcomes", "succeeded")
    assert dependencies["trade_outcomes"]["status"] == "ok"
    assert dependencies["trade_outcomes"]["freshness_threshold_seconds"] == 36 * 60 * 60
    assert dependencies["trade_outcomes"]["window_slack_seconds"] == 300
    assert data["kpis"]["trade_count"] == 0
    assert connection.closed is True


def test_fetch_report_data_rejects_stale_watermark(monkeypatch):
    now = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
    responses = {
        "watermark": [{"completed_at": now - timedelta(hours=37)}],
    }
    cursor, connection = install_fake_database(monkeypatch, responses=responses)
    dependencies = report_service.empty_dependency_status()

    with pytest.raises(report_service.ReportDependencyError, match="watermark is stale"):
        report_service.fetch_report_data(
            "daily",
            "2026-07-12",
            "postgres://example",
            dependency_status=dependencies,
            now=now,
        )

    assert dependencies["trade_outcomes"]["status"] == "stale"
    assert all("FROM trade_outcomes " not in sql for sql, _params in cursor.executions)
    assert connection.closed is True


def test_fetch_report_data_rejects_missing_watermark(monkeypatch):
    cursor, connection = install_fake_database(monkeypatch)
    dependencies = report_service.empty_dependency_status()

    with pytest.raises(report_service.ReportDependencyError, match="no succeeded"):
        report_service.fetch_report_data(
            "daily",
            "2026-07-12",
            "postgres://example",
            dependency_status=dependencies,
        )

    assert dependencies["trade_outcomes"]["status"] == "missing"
    assert all("FROM trade_outcomes " not in sql for sql, _params in cursor.executions)
    assert connection.closed is True


def test_window_bounds_clamps_intraday_to_injected_now():
    now = datetime(2026, 8, 18, 13, 32, tzinfo=timezone.utc)
    start, end = report_service.window_bounds("daily", "2026-08-18", now=now)

    assert end == now
    assert start == now - timedelta(days=1)


def test_window_bounds_keeps_finished_calendar_day():
    now = datetime(2026, 8, 19, 13, 32, tzinfo=timezone.utc)
    start, end = report_service.window_bounds("daily", "2026-08-18", now=now)

    assert end == datetime(2026, 8, 19, tzinfo=timezone.utc)
    assert start == datetime(2026, 8, 18, tzinfo=timezone.utc)


def test_fetch_report_data_rejects_watermark_that_misses_window_tail(monkeypatch):
    now = datetime(2026, 8, 18, 13, 32, tzinfo=timezone.utc)
    responses = {
        "watermark": [{"completed_at": datetime(2026, 8, 18, 0, 30, tzinfo=timezone.utc)}],
    }
    cursor, connection = install_fake_database(monkeypatch, responses=responses)
    dependencies = report_service.empty_dependency_status()

    with pytest.raises(
        report_service.ReportDependencyError,
        match="window tail not materialized",
    ):
        report_service.fetch_report_data(
            "daily",
            "2026-08-18",
            "postgres://example",
            dependency_status=dependencies,
            now=now,
        )

    outcomes = dependencies["trade_outcomes"]
    assert outcomes["status"] == "incomplete"
    assert "window tail not materialized" in outcomes["reason"]
    assert outcomes["window_end"] == now.isoformat()
    assert outcomes["window_slack_seconds"] == 300
    assert all("FROM trade_outcomes " not in sql for sql, _params in cursor.executions)
    assert connection.closed is True


def test_require_fresh_outcome_watermark_records_unmaterialized_tail(monkeypatch):
    now = datetime(2026, 8, 18, 13, 32, tzinfo=timezone.utc)
    cursor, _connection = install_fake_database(
        monkeypatch,
        responses={
            "watermark": [{"completed_at": datetime(2026, 8, 18, 0, 30, tzinfo=timezone.utc)}],
        },
    )
    dependencies = report_service.empty_dependency_status()
    missing = []

    with pytest.raises(
        report_service.ReportDependencyError,
        match="window tail not materialized",
    ):
        report_service.require_fresh_outcome_watermark(
            cursor,
            dependencies,
            now=now,
            window_end=now,
            missing_data=missing,
        )

    assert missing == [dependencies["trade_outcomes"]["reason"]]
    assert "window tail not materialized" in missing[0]


def test_fetch_report_data_accepts_watermark_within_window_slack(monkeypatch):
    now = datetime(2026, 8, 18, 13, 32, tzinfo=timezone.utc)
    responses = {
        "watermark": [{"completed_at": datetime(2026, 8, 18, 13, 30, tzinfo=timezone.utc)}],
    }
    install_fake_database(monkeypatch, responses=responses)
    dependencies = report_service.empty_dependency_status()

    data = report_service.fetch_report_data(
        "daily",
        "2026-08-18",
        "postgres://example",
        dependency_status=dependencies,
        now=now,
    )

    assert dependencies["trade_outcomes"]["status"] == "ok"
    assert dependencies["trade_outcomes"]["window_slack_seconds"] == 300
    assert data["window"]["end"] == now.isoformat()
    assert not any("window tail" in item for item in data["missing_data"])


def test_fetch_report_data_flags_fill_drought_without_attributed_rounds(monkeypatch):
    now = datetime(2026, 8, 18, 13, 32, tzinfo=timezone.utc)
    responses = {
        "watermark": [{"completed_at": datetime(2026, 8, 18, 13, 30, tzinfo=timezone.utc)}],
        "window_fills": [{"fill_count": 12}],
    }
    install_fake_database(monkeypatch, responses=responses)
    dependencies = report_service.empty_dependency_status()

    data = report_service.fetch_report_data(
        "daily",
        "2026-08-18",
        "postgres://example",
        dependency_status=dependencies,
        now=now,
    )

    assert data["outcomes"]["activity"] == {
        "window_robot_fills": 12,
        "attributed_rounds": 0,
    }
    assert (
        "outcome_activity: window_robot_fills=12 attributed_rounds=0"
        in data["missing_data"]
    )


def test_outcome_window_coverage_slack_allows_zero(monkeypatch):
    monkeypatch.setenv("REPORT_OUTCOME_WINDOW_SLACK_SECONDS", "0")
    assert report_service.outcome_window_coverage_slack().total_seconds() == 0


@pytest.mark.parametrize("value", ["-1", "nan", "inf"])
def test_outcome_window_coverage_slack_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv("REPORT_OUTCOME_WINDOW_SLACK_SECONDS", value)

    with pytest.raises(report_service.ReportDependencyError, match="non-negative number"):
        report_service.outcome_window_coverage_slack()


def test_outcome_freshness_threshold_is_configurable(monkeypatch):
    now = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
    monkeypatch.setenv("REPORT_OUTCOME_FRESHNESS_HOURS", "48")
    responses = {
        "watermark": [{"completed_at": now - timedelta(hours=40)}],
    }
    install_fake_database(monkeypatch, responses=responses)
    dependencies = report_service.empty_dependency_status()

    report_service.fetch_report_data(
        "daily",
        "2026-07-12",
        "postgres://example",
        dependency_status=dependencies,
        now=now,
    )

    assert dependencies["trade_outcomes"]["status"] == "ok"
    assert dependencies["trade_outcomes"]["freshness_threshold_seconds"] == 48 * 60 * 60


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_outcome_freshness_threshold_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv("REPORT_OUTCOME_FRESHNESS_HOURS", value)

    with pytest.raises(report_service.ReportDependencyError, match="positive number"):
        report_service.outcome_freshness_threshold()


def test_exchange_state_mirror_error_enters_missing_data(monkeypatch):
    now = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
    responses = {
        "watermark": [{"completed_at": now - timedelta(hours=1)}],
    }
    failures = {
        "exchange_state_mirror": "mirror unavailable",
    }
    install_fake_database(monkeypatch, responses=responses, failures=failures)
    dependencies = report_service.empty_dependency_status()

    data = report_service.fetch_report_data(
        "daily",
        "2026-07-12",
        "postgres://example",
        dependency_status=dependencies,
        now=now,
    )

    assert "exchange_state_mirror: mirror unavailable" in data["missing_data"]
    assert dependencies["exchange_state_mirror"]["status"] == "error"


def test_empty_exchange_state_mirror_fails_closed(monkeypatch):
    now = datetime(2026, 7, 30, 2, tzinfo=timezone.utc)
    responses = {
        "watermark": [{"completed_at": now}],
        "positions_projection": [
            {
                "account_id": "account-a",
                "symbol": "BTCUSDT",
                "quantity": "1",
            }
        ],
    }
    cursor, _connection = install_fake_database(monkeypatch, responses=responses)
    dependencies = report_service.empty_dependency_status()

    data = report_service.fetch_report_data(
        "daily",
        "2026-07-30",
        "postgres://example",
        dependency_status=dependencies,
        now=now,
    )

    mirror = dependencies["exchange_state_mirror"]
    assert mirror["status"] == "missing"
    assert mirror["reason"] == "exchange_state_mirror has no account snapshots"
    assert mirror["reason"] in data["missing_data"]
    assert data["positions"] == []
    assert all(
        "FROM positions_projection" not in sql
        for sql, _params in cursor.executions
    )


def test_fetch_report_data_excludes_stale_exchange_state_mirror(monkeypatch):
    now = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
    responses = {
        "watermark": [{"completed_at": now - timedelta(hours=1)}],
        "exchange_state_mirror": [
            {
                "account_id": "account-a",
                "payload": {
                    "positions": [
                        {
                            "symbol": "ETHUSDT",
                            "position_side": "LONG",
                            "position_amt": "1",
                            "entry_price": "3600",
                            "mark_price": "3800",
                        }
                    ]
                },
                "updated_at": now - timedelta(seconds=60),
            },
            {
                "account_id": "account-b",
                "payload": {
                    "positions": [
                        {
                            "symbol": "BTCUSDT",
                            "position_side": "LONG",
                            "position_amt": "0.25",
                            "entry_price": "110000",
                            "mark_price": "118000",
                        }
                    ]
                },
                "updated_at": now - timedelta(seconds=181),
            }
        ],
        "positions_projection": [
            {
                "account_id": "account-b",
                "symbol": "BTCUSDT",
                "mark_price": "117000",
            }
        ],
    }
    cursor, _connection = install_fake_database(monkeypatch, responses=responses)
    dependencies = report_service.empty_dependency_status()

    data = report_service.fetch_report_data(
        "daily",
        "2026-07-12",
        "postgres://example",
        dependency_status=dependencies,
        now=now,
    )

    mirror = dependencies["exchange_state_mirror"]
    assert mirror["status"] == "stale"
    assert mirror["freshness_threshold_seconds"] == 180
    assert "account-b" in mirror["reason"]
    assert "age_seconds=181" in mirror["reason"]
    assert mirror["reason"] in data["missing_data"]
    assert data["positions"] == []
    assert all("FROM positions_projection" not in sql for sql, _params in cursor.executions)


def test_fetch_report_data_keeps_fresh_exchange_state_mirror(monkeypatch):
    now = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
    updated_at = (now - timedelta(seconds=179)).isoformat().replace("+00:00", "Z")
    responses = {
        "watermark": [{"completed_at": now - timedelta(hours=1)}],
        "exchange_state_mirror": [
            {
                "account_id": "account-a",
                "payload": {
                    "positions": [
                        {
                            "symbol": "BTCUSDT",
                            "position_side": "LONG",
                            "position_amt": "0.25",
                            "entry_price": "110000",
                            "mark_price": "118000",
                        }
                    ]
                },
                "updated_at": updated_at,
            }
        ],
    }
    install_fake_database(monkeypatch, responses=responses)
    dependencies = report_service.empty_dependency_status()

    data = report_service.fetch_report_data(
        "daily",
        "2026-07-12",
        "postgres://example",
        dependency_status=dependencies,
        now=now,
    )

    mirror = dependencies["exchange_state_mirror"]
    assert mirror["status"] == "ok"
    assert mirror["age_seconds"] == 179
    assert mirror["freshness_threshold_seconds"] == 180
    assert data["missing_data"] == []
    assert data["positions"] == [
        {
            "account_id": "account-a",
            "symbol": "BTCUSDT",
            "side": "long",
            "quantity": "0.25",
            "entry_price": "110000",
            "mark_price": "118000",
            "unrealized_pnl": None,
            "updated_at": updated_at,
            "source": "exchange_state_mirror",
        }
    ]


def test_inspect_dependencies_marks_stale_exchange_state_mirror(monkeypatch):
    now = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
    responses = {
        "watermark": [{"completed_at": now - timedelta(hours=1)}],
        "exchange_state_mirror": [
            {
                "account_id": "account-b",
                "updated_at": now - timedelta(seconds=181),
            }
        ],
    }
    cursor, connection = install_fake_database(monkeypatch, responses=responses)

    dependencies = report_service.inspect_dependencies(
        "postgres://example",
        now=now,
    )

    mirror = dependencies["exchange_state_mirror"]
    assert mirror["status"] == "stale"
    assert mirror["freshness_threshold_seconds"] == 180
    assert "account-b" in mirror["reason"]
    assert report_service.service_status(dependencies) == "degraded"
    assert any(
        "SELECT account_id, updated_at FROM exchange_state_mirror" in sql
        for sql, _params in cursor.executions
    )
    assert connection.closed is True


def test_report_api_records_success_in_health(monkeypatch):
    dependencies = healthy_dependencies()

    def fake_fetch(_report_type, _report_date, dependency_status=None, **_kwargs):
        dependency_status.update(dependencies)
        return report_service.empty_db_data()

    monkeypatch.setattr(report_service, "fetch_report_data", fake_fetch)
    monkeypatch.setattr(
        report_service,
        "write_report",
        lambda _payload, _data: {
            "url": "https://hk.balen.wang/reports/report.html",
            "path": "/tmp/report.html",
        },
    )
    monkeypatch.setattr(report_service, "inspect_dependencies", lambda: dependencies)
    app = report_service.create_app()
    create_report = app_endpoint(app, "/reports", "POST")
    healthz = app_endpoint(app, "/healthz", "GET")

    result = create_report(base_payload())
    health = healthz()

    assert result["url"] == "https://hk.balen.wang/reports/report.html"
    assert health["status"] == "ok"
    assert health["dependencies"]["trade_outcomes"]["status"] == "ok"
    assert health["last_publication"]["status"] == "succeeded"
    assert health["last_publication"]["url"] == result["url"]


def test_report_api_returns_503_and_records_dependency_failure(monkeypatch):
    dependencies = healthy_dependencies()
    dependencies["trade_outcomes"]["status"] = "stale"
    dependencies["trade_outcomes"]["reason"] = "trade_outcomes watermark is stale"

    def fake_fetch(_report_type, _report_date, dependency_status=None, **_kwargs):
        dependency_status.update(dependencies)
        raise report_service.ReportDependencyError(
            "trade_outcomes",
            "trade_outcomes watermark is stale",
        )

    monkeypatch.setattr(report_service, "fetch_report_data", fake_fetch)
    monkeypatch.setattr(report_service, "inspect_dependencies", lambda: dependencies)
    app = report_service.create_app()
    create_report = app_endpoint(app, "/reports", "POST")
    healthz = app_endpoint(app, "/healthz", "GET")

    with pytest.raises(report_service.HTTPException) as raised:
        create_report(base_payload())

    health = healthz()
    assert raised.value.status_code == 503
    assert raised.value.detail["code"] == "report_dependency_unavailable"
    assert raised.value.detail["dependency"] == "trade_outcomes"
    assert health["status"] == "unhealthy"
    assert health["last_publication"]["status"] == "failed"
    assert health["last_publication"]["reason"] == "trade_outcomes watermark is stale"


def test_report_api_returns_500_and_records_write_failure(monkeypatch):
    dependencies = healthy_dependencies()

    def fake_fetch(_report_type, _report_date, dependency_status=None, **_kwargs):
        dependency_status.update(dependencies)
        return report_service.empty_db_data()

    def fail_write(_payload, _data):
        raise OSError("report directory is read-only")

    monkeypatch.setattr(report_service, "fetch_report_data", fake_fetch)
    monkeypatch.setattr(report_service, "write_report", fail_write)
    monkeypatch.setattr(report_service, "inspect_dependencies", lambda: dependencies)
    app = report_service.create_app()
    create_report = app_endpoint(app, "/reports", "POST")
    healthz = app_endpoint(app, "/healthz", "GET")

    with pytest.raises(report_service.HTTPException) as raised:
        create_report(base_payload())

    health = healthz()
    assert raised.value.status_code == 500
    assert raised.value.detail["code"] == "report_publication_failed"
    assert health["status"] == "degraded"
    assert health["last_publication"]["error_code"] == "report_publication_failed"
    assert "read-only" in health["last_publication"]["reason"]


def test_compute_account_closeouts_always_emits_four_canonical_accounts():
    closeouts = report_service.compute_account_closeouts([], [], [], {})

    assert [row["account_id"] for row in closeouts] == list(report_service.REPORT_ACCOUNT_IDS)
    assert all(row["trade_count"] == 0 and row["position_count"] == 0 for row in closeouts)
    assert all(row["period_pnl"] == 0 and row["unrealized_pnl"] == 0 for row in closeouts)


def test_compute_account_closeouts_splits_pnl_and_keeps_empty_accounts():
    closeouts = report_service.compute_account_closeouts(
        [
            {"account_id": "account-a", "closed_at": "2026-08-26T10:00:00+00:00", "instrument_id": "BTCUSDT", "side": "long", "realized_pnl": 12.5, "r_multiple": 1.0},
            {"account_id": "account-c", "closed_at": "2026-08-26T11:00:00+00:00", "instrument_id": "ETHUSDT", "side": "short", "realized_pnl": -3.0, "r_multiple": -0.5},
        ],
        [
            {
                "account_id": "account-b",
                "symbol": "SOLUSDT",
                "side": "long",
                "quantity": "2",
                "unrealized_pnl": "4.2",
            }
        ],
        [
            {"account_id": "account-a", "equity": "1000", "available_balance": "200"},
            {"account_id": "account-d", "equity": "2500", "available_balance": "2500"},
        ],
        {"account-a": 5, "account-b": 1},
    )
    by_id = {row["account_id"]: row for row in closeouts}

    assert list(by_id) == list(report_service.REPORT_ACCOUNT_IDS)
    assert by_id["account-a"]["period_pnl"] == 12.5
    assert by_id["account-a"]["trade_count"] == 1
    assert by_id["account-a"]["open_order_count"] == 5
    assert by_id["account-a"]["equity"] == 1000.0
    assert by_id["account-b"]["position_count"] == 1
    assert by_id["account-b"]["unrealized_pnl"] == 4.2
    assert by_id["account-b"]["trade_count"] == 0
    assert by_id["account-c"]["period_pnl"] == -3.0
    assert by_id["account-c"]["position_count"] == 0
    assert by_id["account-d"]["equity"] == 2500.0
    assert by_id["account-d"]["trade_count"] == 0
    assert by_id["account-d"]["position_count"] == 0


def test_fetch_report_data_builds_four_account_closeouts(monkeypatch):
    now = datetime(2026, 8, 26, 13, 30, tzinfo=timezone.utc)
    responses = {
        "watermark": [{"completed_at": now - timedelta(minutes=5)}],
        "trade_outcomes": [
            {
                "account_id": "account-a",
                "closed_at": now - timedelta(hours=2),
                "instrument_id": "BTCUSDT",
                "side": "long",
                "realized_pnl": 8.0,
                "r_multiple": 0.8,
            }
        ],
        "exchange_state_mirror": [
            {
                "account_id": "account-a",
                "payload": {
                    "positions": [
                        {
                            "symbol": "BTCUSDT",
                            "position_side": "LONG",
                            "position_amt": "0.1",
                            "entry_price": "110000",
                            "mark_price": "111000",
                            "unrealized_pnl": "10",
                        }
                    ],
                    "open_orders": [{"symbol": "BTCUSDT"}],
                    "algo_orders": [{"symbol": "BTCUSDT"}],
                },
                "updated_at": now - timedelta(seconds=20),
            },
            {
                "account_id": "account-b",
                "payload": {"positions": [], "open_orders": [], "algo_orders": []},
                "updated_at": now - timedelta(seconds=20),
            },
            {
                "account_id": "account-c",
                "payload": {"positions": [], "open_orders": [], "algo_orders": []},
                "updated_at": now - timedelta(seconds=20),
            },
            {
                "account_id": "account-d",
                "payload": {"positions": [], "open_orders": [], "algo_orders": []},
                "updated_at": now - timedelta(seconds=20),
            },
        ],
        "accounts_projection": [
            {"account_id": "account-a", "equity": "5200", "available_balance": "1000"},
            {"account_id": "account-b", "equity": "4800", "available_balance": "4800"},
            {"account_id": "account-c", "equity": "2500", "available_balance": "2500"},
            {"account_id": "account-d", "equity": "2500", "available_balance": "2500"},
        ],
    }
    install_fake_database(monkeypatch, responses=responses)
    dependencies = report_service.empty_dependency_status()

    data = report_service.fetch_report_data(
        "daily",
        "2026-08-26",
        "postgres://example",
        dependency_status=dependencies,
        now=now,
    )

    assert [row["account_id"] for row in data["accounts"]] == list(report_service.REPORT_ACCOUNT_IDS)
    by_id = {row["account_id"]: row for row in data["accounts"]}
    assert by_id["account-a"]["period_pnl"] == 8.0
    assert by_id["account-a"]["position_count"] == 1
    assert by_id["account-a"]["open_order_count"] == 2
    assert by_id["account-a"]["equity"] == 5200.0
    assert by_id["account-b"]["trade_count"] == 0
    assert by_id["account-c"]["position_count"] == 0
    assert by_id["account-d"]["equity"] == 2500.0


def test_html_renders_four_account_closeouts():
    html = report_service.render_report_html(
        base_payload(),
        report_service.sample_db_data(),
    )

    assert "四账户收盘" in html
    assert "account-a" in html
    assert "account-b" in html
    assert "account-c" in html
    assert "account-d" in html
    assert "今日无平仓" in html
    assert "持仓敞口" not in html


def test_html_injection_escapes_script_end():
    payload = base_payload()
    payload["title"] = "</script><script>alert(1)</script>"
    html = report_service.render_report_html(payload, report_service.empty_db_data())

    assert "window.REPORT_DATA =" in html
    assert "</script><script>alert(1)</script>" not in html
    assert "<\\/script>" in html


def test_write_report_uses_random_tokenized_filename(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE", "https://hk.balen.wang")
    payload = base_payload()

    first = report_service.write_report(payload, report_service.empty_db_data(), tmp_path)
    second = report_service.write_report(payload, report_service.empty_db_data(), tmp_path)

    assert first["path"] != second["path"]
    assert Path(first["path"]).name.startswith("2026-07-12-daily-")
    assert first["url"].startswith("https://hk.balen.wang/reports/")


def test_asset_validation_accepts_only_supported_images_under_10mb():
    assert report_service.validate_asset("image/png", 10) == ".png"
    assert report_service.validate_asset("image/jpeg", 10) == ".jpg"
    assert report_service.validate_asset("image/webp", 10) == ".webp"

    with pytest.raises(report_service.ReportValidationError):
        report_service.validate_asset("image/gif", 10)

    with pytest.raises(report_service.ReportValidationError):
        report_service.validate_asset("image/jpeg", 10 * 1024 * 1024 + 1)


def test_json_for_script_never_contains_raw_script_end():
    text = report_service.json_for_script({"x": "</script><script>alert(1)</script>"})

    assert "</script>" not in text.lower()
    assert "<\\/script>" in text
