from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from uuid import UUID


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
sys.path.insert(0, str(SERVICE_ROOT))

from routing import (  # noqa: E402
    AccountRoute,
    MultiAccountRouteTable,
    RouteConflictError,
    UnknownAccountError,
    WrongAccountError,
)


INTENT_ID = UUID("11111111-2222-3333-4444-555555555555")


class MultiAccountRoutingIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.spool_root = Path(self._tmpdir.name) / "spool"
        self.route_a = AccountRoute(
            account_id="account-a",
            node_id="nautilus-node-account-a",
            redis_key_prefix="nautilus:account-a:node-a",
            control_plane_base_url="http://control-plane:8080",
            spool_root=self.spool_root,
        )
        self.route_b = AccountRoute(
            account_id="account-b",
            node_id="nautilus-node-account-b",
            redis_key_prefix="nautilus:account-b:node-b",
            control_plane_base_url="http://control-plane:8080",
            spool_root=self.spool_root,
        )
        self.table = MultiAccountRouteTable([self.route_a, self.route_b])

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_intent_routes_only_to_target_account_node(self) -> None:
        intent = {"intent_id": str(INTENT_ID), "account_id": "account-a"}

        routed = self.table.route_intent(intent)

        self.assertEqual(routed, self.route_a)
        self.assertEqual(
            self.table.assert_intent_for_node(intent, "nautilus-node-account-a"),
            self.route_a,
        )
        with self.assertRaises(WrongAccountError):
            self.table.assert_intent_for_node(intent, "nautilus-node-account-b")
        with self.assertRaises(UnknownAccountError):
            self.table.route_intent({"intent_id": str(INTENT_ID), "account_id": "missing"})

    def test_order_redis_spool_and_event_namespaces_do_not_overlap(self) -> None:
        order_a = self.route_a.client_order_ref(INTENT_ID, sequence=1)
        order_b = self.route_b.client_order_ref(INTENT_ID, sequence=1)

        self.assertEqual(order_a.client_order_id, order_b.client_order_id)
        self.assertNotEqual(order_a.namespace_key, order_b.namespace_key)
        self.assertIn("account_id=account-a", order_a.tags)
        self.assertIn("account_id=account-b", order_b.tags)
        self.assertTrue(order_a.namespace_key.startswith("client-order-id:account-a:"))
        self.assertTrue(order_b.namespace_key.startswith("client-order-id:account-b:"))
        self.assertNotEqual(
            self.route_a.redis_key("idempotency", "fills", "venue-1"),
            self.route_b.redis_key("idempotency", "fills", "venue-1"),
        )
        self.assertNotEqual(self.route_a.spool_path, self.route_b.spool_path)
        self.assertIn("account-a", self.route_a.spool_path.parts)
        self.assertIn("account-b", self.route_b.spool_path.parts)
        self.assertNotEqual(
            self.route_a.event_route_key("event-1"),
            self.route_b.event_route_key("event-1"),
        )
        self.assertNotEqual(
            self.route_a.event_stream("OrderFilled"),
            self.route_b.event_stream("OrderFilled"),
        )

    def test_one_node_outage_does_not_mutate_other_route_or_namespace(self) -> None:
        before_b = (
            self.route_b.redis_key("state", "cursor"),
            self.route_b.spool_path,
            self.route_b.event_stream("AccountState"),
        )

        self.table.mark_node_unavailable("nautilus-node-account-a", "container stopped")

        self.assertEqual(self.table.node_status("nautilus-node-account-a"), "unavailable")
        self.assertEqual(self.table.node_status("nautilus-node-account-b"), "running")
        self.assertEqual(self.table.route_intent({"account_id": "account-b"}), self.route_b)
        self.assertEqual(
            before_b,
            (
                self.route_b.redis_key("state", "cursor"),
                self.route_b.spool_path,
                self.route_b.event_stream("AccountState"),
            ),
        )

    def test_routing_table_update_replaces_account_to_node_mapping(self) -> None:
        replacement = AccountRoute(
            account_id="account-a",
            node_id="nautilus-node-account-a-v2",
            redis_key_prefix="nautilus:account-a:node-a-v2",
            control_plane_base_url="http://control-plane:8080",
            spool_root=self.spool_root,
        )

        self.table.upsert_route(replacement)

        self.assertEqual(self.table.route_for_account("account-a"), replacement)
        self.assertEqual(
            self.table.assert_intent_for_node(
                {"account_id": "account-a"},
                "nautilus-node-account-a-v2",
            ),
            replacement,
        )
        with self.assertRaises(WrongAccountError):
            self.table.assert_intent_for_node(
                {"account_id": "account-a"},
                "nautilus-node-account-a",
            )

    def test_halt_command_routes_to_one_account_or_all_nodes(self) -> None:
        account_halt = self.table.route_command("halt", account_id="account-a")
        all_halt = self.table.route_command("halt", all_nodes=True)

        self.assertEqual(account_halt.node_ids, ("nautilus-node-account-a",))
        self.assertEqual(account_halt.account_ids, ("account-a",))
        self.assertEqual(
            all_halt.node_ids,
            ("nautilus-node-account-a", "nautilus-node-account-b"),
        )
        self.assertEqual(all_halt.account_ids, ("account-a", "account-b"))

    def test_route_table_rejects_shared_mutable_namespaces(self) -> None:
        duplicate_prefix = AccountRoute(
            account_id="account-c",
            node_id="nautilus-node-account-a",
            redis_key_prefix="nautilus:account-c:node-a",
            control_plane_base_url="http://control-plane:8080",
            spool_root=self.spool_root,
            spool_file_name=self.route_a.spool_path.name,
        )

        with self.assertRaises(RouteConflictError):
            MultiAccountRouteTable([self.route_a, duplicate_prefix])


if __name__ == "__main__":
    unittest.main()
