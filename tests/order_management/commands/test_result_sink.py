from __future__ import annotations

from commands.result_sink import CommandResultSink


class FlakyControlPlane:
    def __init__(self):
        self.calls = []
        self.fail_next = True

    def ack_command(self, node_id, command_id, status, result=None, error=None):
        self.calls.append((node_id, command_id, str(status), result, error))
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("ack unavailable")


def test_result_sink_retries_ack_and_keeps_per_item_results_queryable():
    control_plane = FlakyControlPlane()
    sink = CommandResultSink(control_plane, node_id="om6-node-a", max_ack_attempts=2, sleep=lambda _seconds: None)

    sink.record_running(
        "cmd-1",
        result={"dispatched": True},
        items=[{"item_id": "order-1", "status": "running"}],
    )

    assert len(control_plane.calls) == 2
    assert sink.get_result("cmd-1")["status"] == "running"
    assert sink.get_item_result("cmd-1", "order-1") == {"item_id": "order-1", "status": "running"}

    sink.record_completed("cmd-1", result={"verified": True})
    completed_ack_count = len(control_plane.calls)
    sink.record_completed("cmd-1", result={"verified": True})

    assert sink.get_result("cmd-1")["status"] == "completed"
    assert len(control_plane.calls) == completed_ack_count
