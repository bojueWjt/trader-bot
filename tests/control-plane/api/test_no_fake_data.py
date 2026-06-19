from __future__ import annotations

import sys
from pathlib import Path

import pytest

BRIDGE_API = Path(__file__).resolve().parents[3] / "bridge" / "apps" / "api"
if str(BRIDGE_API) not in sys.path:
    sys.path.insert(0, str(BRIDGE_API))


def _reimport_provider():
    for mod in ("app.services.dashboard_provider", "app.services.dashboard_fake_adapter"):
        sys.modules.pop(mod, None)


def test_production_dashboard_uses_empty_adapter_not_fixtures(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    _reimport_provider()
    from app.services.dashboard_fake_adapter import DashboardFakeAdapter
    from app.services.dashboard_provider import EmptyDashboardAdapter, dashboard_adapter

    adapter = dashboard_adapter()
    assert isinstance(adapter, EmptyDashboardAdapter)
    assert adapter.overview()["risk_state"] == "unavailable"
    assert adapter.open_trades() == {"open_trades": []}
    assert adapter.events() == {"events": []}

    with pytest.raises(RuntimeError):
        DashboardFakeAdapter()  # fixtures are test-only


def test_test_env_allows_fixture_adapter(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    _reimport_provider()
    from app.services.dashboard_fake_adapter import DashboardFakeAdapter
    from app.services.dashboard_provider import dashboard_adapter

    assert isinstance(dashboard_adapter(), DashboardFakeAdapter)
