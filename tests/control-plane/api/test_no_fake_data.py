from __future__ import annotations

import sys
from pathlib import Path

import pytest

BRIDGE_API = Path(__file__).resolve().parents[3] / "bridge" / "apps" / "api"


def _prefer_bridge_app() -> None:
    bridge = str(BRIDGE_API)
    if bridge in sys.path:
        sys.path.remove(bridge)
    sys.path.insert(0, bridge)
    app_mod = sys.modules.get("app")
    app_file = str(getattr(app_mod, "__file__", "") or "")
    if app_mod is not None and not app_file.startswith(bridge):
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                sys.modules.pop(name, None)


_prefer_bridge_app()


def _reimport_provider():
    _prefer_bridge_app()
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
