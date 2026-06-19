from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


BRIDGE_ROOT = Path(__file__).resolve().parents[1]
for _path in (
    BRIDGE_ROOT,
    BRIDGE_ROOT / "apps" / "api",
    BRIDGE_ROOT / "user_data" / "strategies",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

BRIDGE_TEST_ENV = {
    "AUTH_SECRET_KEY": "unit-auth-secret",
    "RISK_ADMIN_TOKEN": "unit-risk-admin-token",
    "VIEWER_TOKEN": "unit-viewer-token",
    "REVIEWER_TOKEN": "unit-reviewer-token",
    "SYSTEM_OBSERVER_TOKEN": "unit-system-observer-token",
    "NAUTILUS_NODE_TOKEN": "unit-nautilus-node-token",
    "RISK_ADMIN_API_TOKEN": "unit-risk-admin-token",
    "VIEWER_API_TOKEN": "unit-viewer-token",
    "REVIEWER_API_TOKEN": "unit-reviewer-token",
    "SYSTEM_OBSERVER_API_TOKEN": "unit-system-observer-token",
    "NAUTILUS_NODE_API_TOKEN": "unit-nautilus-node-token",
}

for _name, _value in BRIDGE_TEST_ENV.items():
    os.environ.setdefault(_name, _value)


@pytest.fixture(autouse=True)
def bridge_security_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in BRIDGE_TEST_ENV.items():
        monkeypatch.setenv(name, value)
