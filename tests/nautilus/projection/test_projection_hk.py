from __future__ import annotations

import unittest


class ProjectionHostNautilusTests(unittest.TestCase):
    @unittest.skip("TODO(host-verify): run in hk Nautilus container")
    def test_projection_actor_receives_real_nautilus_on_event_callbacks(self) -> None:
        """Verify real Nautilus Actor/Strategy wiring and event class names.

        Host checklist:
        - ProjectionActor can be wired as a Nautilus Actor or equivalent runtime
          component with ``on_event`` callbacks.
        - Real order events expose ``ts_event``, ``client_order_id``,
          ``venue_order_id``, ``trade_id``, and ``instrument_id`` as assumed.
        - Real position/account event classes map to ``PositionOpened``,
          ``PositionChanged``, ``PositionClosed``, and ``AccountState`` or the
          catalog is updated with verified class names.
        """


if __name__ == "__main__":
    unittest.main()
