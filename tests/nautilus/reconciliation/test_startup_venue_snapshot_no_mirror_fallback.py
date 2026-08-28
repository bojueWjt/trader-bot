"""Batch 1.1 P1-7: startup venue snapshot must never fall back to the mirror.

The control-plane exchange-state mirror's refresh() can trigger the fatal
fence on a 409 (process exit, uncatchable by the caller) and only surfaces
orders, not positions. _startup_venue_instrument_ids therefore uses the
exchange evidence provider only; when the provider is missing or failing it
returns None with a WARNING and must not touch mirror.refresh().
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
for _path in (str(SERVICE_ROOT), str(EXECUTION_DOMAIN_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from app.node import _startup_venue_instrument_ids  # noqa: E402


class _RecordingMirror:
    def __init__(self) -> None:
        self.refresh_calls = 0

    def refresh(self):
        self.refresh_calls += 1
        raise AssertionError(
            "mirror.refresh() must never be called from the startup "
            "venue-snapshot path (fatal-fence hazard)"
        )


def _runtime(*, provider, mirror, environment: str = "live") -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(binance=SimpleNamespace(environment=environment)),
        exchange_evidence_provider=provider,
        exchange_state_mirror=mirror,
    )


class StartupVenueSnapshotNoMirrorFallbackTests(unittest.TestCase):
    def test_provider_missing_returns_none_without_touching_mirror(self) -> None:
        mirror = _RecordingMirror()

        result = _startup_venue_instrument_ids(
            _runtime(provider=None, mirror=mirror)
        )

        self.assertIsNone(result)
        self.assertEqual(mirror.refresh_calls, 0)

    def test_provider_failure_returns_none_without_touching_mirror(self) -> None:
        mirror = _RecordingMirror()

        class _FailingProvider:
            def cached_snapshot(self, max_age_seconds):
                return None

            def snapshot(self, force_refresh=False):
                raise RuntimeError("venue evidence unavailable")

        result = _startup_venue_instrument_ids(
            _runtime(provider=_FailingProvider(), mirror=mirror)
        )

        self.assertIsNone(result)
        self.assertEqual(mirror.refresh_calls, 0)

    def test_provider_snapshot_still_yields_instrument_ids(self) -> None:
        mirror = _RecordingMirror()

        class _Provider:
            def cached_snapshot(self, max_age_seconds):
                return None

            def snapshot(self, force_refresh=False):
                return {
                    "positions": [{"symbol": "atomusdt"}],
                    "regular_orders": [{"symbol": "BTCUSDT"}],
                    "algo_orders": [],
                }

        result = _startup_venue_instrument_ids(
            _runtime(provider=_Provider(), mirror=mirror)
        )

        self.assertEqual(
            result,
            ["ATOMUSDT-PERP.BINANCE", "BTCUSDT-PERP.BINANCE"],
        )
        self.assertEqual(mirror.refresh_calls, 0)


if __name__ == "__main__":
    unittest.main()
