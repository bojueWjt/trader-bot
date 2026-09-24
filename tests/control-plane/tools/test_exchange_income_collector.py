from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = (
    REPO_ROOT / "services" / "control-plane" / "tools" / "exchange_income_collector.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "_exchange_income_collector_under_test", MODULE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_paginate_income_dedups_and_completes_on_short_page():
    module = _load_module()
    pages = {
        1: [
            {
                "tranId": i,
                "incomeType": "REALIZED_PNL",
                "asset": "USDT",
                "time": 1,
                "symbol": "BTCUSDT",
                "income": "1",
            }
            for i in range(1000)
        ]
        + [
            {
                "tranId": 0,
                "incomeType": "REALIZED_PNL",
                "asset": "USDT",
                "time": 1,
                "symbol": "BTCUSDT",
                "income": "1",
            }
        ],
        2: [
            {
                "tranId": 1000,
                "incomeType": "COMMISSION",
                "asset": "BNB",
                "time": 2,
                "symbol": "",
                "income": "-0.1",
            }
        ],
    }

    def fetch_page(page: int):
        return pages[page]

    rows, complete = module.paginate_income(fetch_page, limit=1000)
    assert complete is True
    assert len(rows) == 1001


def test_paginate_income_incomplete_when_pages_exhausted():
    module = _load_module()

    def fetch_page(page: int):
        return [{"tranId": page, "incomeType": "FUNDING_FEE", "asset": "USDT", "time": page, "symbol": "", "income": "0.01"}] * 1000

    rows, complete = module.paginate_income(fetch_page, limit=1000, max_pages=2)
    assert complete is False
    assert len(rows) == 2


def test_coverage_contains_adjacent_windows():
    module = _load_module()
    start = datetime(2026, 9, 23, tzinfo=timezone.utc)
    end = datetime(2026, 9, 24, tzinfo=timezone.utc)
    rows = [
        {
            "account_id": "account-d",
            "window_start": start,
            "window_end": datetime(2026, 9, 23, 12, tzinfo=timezone.utc),
            "complete": True,
        },
        {
            "account_id": "account-d",
            "window_start": datetime(2026, 9, 23, 12, tzinfo=timezone.utc),
            "window_end": end,
            "complete": True,
        },
    ]
    assert module.coverage_contains(
        rows, account_id="account-d", window_start=start, window_end=end
    )
    rows[1]["complete"] = False
    assert not module.coverage_contains(
        rows, account_id="account-d", window_start=start, window_end=end
    )


def test_collect_account_window_splits_on_page_overflow_and_multi_asset():
    module = _load_module()
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    end = datetime(2026, 9, 2, tzinfo=timezone.utc)
    calls = []

    def fetch_page(window_start, window_end, page):
        calls.append((window_start, window_end, page))
        if (window_start, window_end) == (start, end):
            return [
                {
                    "tranId": f"{page}-{i}",
                    "incomeType": "REALIZED_PNL",
                    "asset": "USDT",
                    "time": int(window_start.timestamp() * 1000),
                    "symbol": "BTCUSDT",
                    "income": "1",
                }
                for i in range(1000)
            ]
        return [
            {
                "tranId": f"{window_start.isoformat()}-{page}-usdt",
                "incomeType": "FUNDING_FEE",
                "asset": "USDT",
                "time": int(window_start.timestamp() * 1000) + 1,
                "symbol": "",
                "income": "0.2",
            },
            {
                "tranId": f"{window_start.isoformat()}-{page}-bnb",
                "incomeType": "COMMISSION",
                "asset": "BNB",
                "time": int(window_start.timestamp() * 1000) + 2,
                "symbol": "ETHUSDT",
                "income": "-0.01",
            },
        ]

    rows, complete, error = module.collect_account_window(
        account_id="account-a",
        start=start,
        end=end,
        fetch_page=fetch_page,
        max_pages=2,
        now=datetime(2026, 9, 23, tzinfo=timezone.utc),
    )
    assert complete is True
    assert error is None
    assets = {row["asset"] for row in rows}
    assert "USDT" in assets
    assert "BNB" in assets
    assert any(span_call[1] - span_call[0] < (end - start) for span_call in calls)


def test_log_redacts_key_material():
    module = _load_module()
    messages = []
    module.esr.log = lambda msg: messages.append(msg)
    module.log("api_key=abc signature=deadbeef")
    assert messages == ["redacted_error"]


def test_run_once_does_not_fetch_when_keys_missing():
    module = _load_module()

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args, **_kwargs):
            return None

    class _Conn:
        def commit(self):
            return None

        def cursor(self):
            return _Cur()

    called = []

    def signed_get(*_args, **_kwargs):
        called.append("signed")
        raise AssertionError("collector must not call binance without keys")

    now = datetime(2026, 9, 23, 18, tzinfo=timezone.utc)
    summary = module.run_once(
        _Conn(),
        start=datetime(2026, 9, 22, tzinfo=timezone.utc),
        end=datetime(2026, 9, 23, tzinfo=timezone.utc),
        base="https://fapi.binance.com",
        signed_get=signed_get,
        keys_for_account=lambda _account: None,
        opener_factory=lambda _account: None,
        now=now,
    )
    assert called == []
    assert summary["complete"] is False
    assert summary["accounts"]["account-d"]["reason"] == "keys_unavailable"


def test_malformed_or_none_or_out_of_window_fails_coverage():
    module = _load_module()
    start = datetime(2026, 9, 22, tzinfo=timezone.utc)
    end = datetime(2026, 9, 23, tzinfo=timezone.utc)
    now = datetime(2026, 9, 23, 1, tzinfo=timezone.utc)

    def none_page(*_args):
        return None

    rows, complete, error = module.collect_account_window(
        account_id="account-a", start=start, end=end, fetch_page=none_page, now=now
    )
    assert complete is False
    assert rows == []
    assert "income_response_none" in str(error)

    def bad_row(*_args):
        return ["not-a-dict"]

    _, complete, error = module.collect_account_window(
        account_id="account-a", start=start, end=end, fetch_page=bad_row, now=now
    )
    assert complete is False
    assert "income_row_not_object" in str(error)

    def outside(*_args):
        return [
            {
                "tranId": "1",
                "incomeType": "REALIZED_PNL",
                "asset": "USDT",
                "time": int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp() * 1000),
                "symbol": "BTCUSDT",
                "income": "1",
            }
        ]

    _, complete, error = module.collect_account_window(
        account_id="account-a", start=start, end=end, fetch_page=outside, now=now
    )
    assert complete is False
    assert "income_time_outside_window" in str(error)


def test_invalid_windows_cannot_mark_complete():
    module = _load_module()
    now = datetime(2026, 9, 23, tzinfo=timezone.utc)
    rows, complete, error = module.collect_account_window(
        account_id="account-a",
        start=now,
        end=now,
        fetch_page=lambda *_args: [],
        now=now,
    )
    assert complete is False and rows == []
    assert error == "window_end_not_after_start"
    rows, complete, error = module.collect_account_window(
        account_id="account-a",
        start=now,
        end=now + timedelta(hours=1),
        fetch_page=lambda *_args: [],
        now=now,
    )
    assert complete is False
    assert error == "window_end_in_future"
    rows, complete, error = module.collect_account_window(
        account_id="account-a",
        start=now - timedelta(days=100),
        end=now - timedelta(days=99),
        fetch_page=lambda *_args: [],
        now=now,
    )
    assert complete is False
    assert error == "window_outside_90d_retention"


def test_signed_page_factory_passes_opener_keyword():
    module = _load_module()
    seen = {}

    def signed_get(*args, **kwargs):
        seen["args_len"] = len(args)
        seen["opener"] = kwargs.get("opener")
        return []

    fetch = module._signed_page_factory(
        "https://fapi.binance.com", "k", "s", "OP", signed_get
    )
    fetch(
        datetime(2026, 9, 22, tzinfo=timezone.utc),
        datetime(2026, 9, 23, tzinfo=timezone.utc),
        1,
    )
    assert seen["opener"] == "OP"


def test_default_window_matches_report():
    module = _load_module()
    sys_path_parent = Path(MODULE_PATH).parents[3] / "hermes-profile" / "scripts"
    import sys

    sys.path.insert(0, str(sys_path_parent))
    import report_data

    now = datetime(2026, 9, 23, 18, tzinfo=timezone.utc)
    assert module.completed_utc_window(1, now) == report_data.completed_utc_window(1, now)
    for days in (0, -1):
        try:
            module.completed_utc_window(days, now)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid days must reject")
        try:
            report_data.completed_utc_window(days, now)
        except ValueError:
            pass
        else:
            raise AssertionError("report invalid days must reject")
