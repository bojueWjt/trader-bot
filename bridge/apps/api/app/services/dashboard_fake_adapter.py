from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.contracts.dashboard import (
    DashboardEventsResponse,
    DashboardOpenTradesResponse,
    DashboardOverview,
)


class DashboardFakeAdapter:
    def __init__(self, fixture_path: Path | None = None):
        self.fixture_path = fixture_path or self._default_fixture_path()

    def overview(self) -> DashboardOverview:
        snapshot = self._snapshot()
        return snapshot["overview"]

    def open_trades(self) -> DashboardOpenTradesResponse:
        snapshot = self._snapshot()
        return {"open_trades": snapshot["open_trades"]}

    def events(self) -> DashboardEventsResponse:
        snapshot = self._snapshot()
        return {"events": snapshot["events"]}

    def _snapshot(self) -> dict[str, Any]:
        with self.fixture_path.open(encoding="utf-8") as fixture_file:
            data = json.load(fixture_file)

        return data

    def _default_fixture_path(self) -> Path:
        return Path(__file__).resolve().parents[4] / "fixtures" / "dashboard" / "snapshot.json"
