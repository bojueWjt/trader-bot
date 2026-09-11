import re
import runpy
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
GENERATOR = REPO_ROOT / "scripts" / "hk-gen-recreate-patched.py"
HARDENING_DEPLOY = REPO_ROOT / "scripts" / "hk-deploy-20260803.sh"
ROOT_WINDOW = REPO_ROOT / "scripts" / "hk-root-window-20260729.sh"
ROOT_CONTINUE = REPO_ROOT / "scripts" / "hk-root-continue-20260729.sh"
BINANCE_DST = (
    "/usr/local/lib/python3.12/site-packages/"
    "nautilus_trader/adapters/binance/execution.py"
)
BINANCE_FUTURES_DST = (
    "/usr/local/lib/python3.12/site-packages/"
    "nautilus_trader/adapters/binance/futures/execution.py"
)
EXPECTED_PATCH_MOUNTS = {
    "entry_batch.py": "/app/execution_domain/entry_batch.py",
    "account_execution_ledger.py": "/app/execution_domain/account_execution_ledger.py",
    "execution_domain_init.py": "/app/execution_domain/__init__.py",
    "idempotency.py": "/app/execution_domain/idempotency.py",
    "identifiers.py": "/app/execution_domain/identifiers.py",
    "owned_order_recovery.py": "/app/runtime/owned_order_recovery.py",
    "intent_execution_planner.py": "/app/strategy/intent_execution_planner.py",
    "contracts.py": "/app/execution_domain/contracts.py",
    "control_plane.py": "/app/execution_domain/control_plane.py",
    "portfolio_baseline.py": "/app/execution_domain/portfolio_baseline.py",
    "order_ownership.py": "/app/execution_domain/order_ownership.py",
    "http_client.py": "/app/execution_domain/http_client.py",
    "projection_actor.py": "/app/projection/actor.py",
    "projection_spool.py": "/app/projection/spool.py",
    "event_mapper.py": "/app/projection/event_mapper.py",
    "intent_execution_strategy.py": "/app/strategy/intent_execution_strategy.py",
    "exchange_cancel_adapter.py": "/app/runtime/exchange_cancel_adapter.py",
    "lifecycle.py": "/app/runtime/lifecycle.py",
    "health.py": "/app/runtime/health.py",
    "bounded_task_worker.py": "/app/runtime/bounded_task_worker.py",
    "control_plane_session.py": "/app/runtime/control_plane_session.py",
    "intent_execution_inbox.py": "/app/runtime/intent_execution_inbox.py",
    "redis_safety.py": "/app/runtime/redis_safety.py",
    "live_canary_execution.py": "/app/runtime/live_canary_execution.py",
    "reconciliation.py": "/app/runtime/reconciliation.py",
    "nautilus_reconciliation_scope.py": (
        "/app/runtime/nautilus_reconciliation_scope.py"
    ),
    "binance_adapter_config.py": "/app/runtime/binance_adapter_config.py",
    "node.py": "/app/app/node.py",
    "run_node.py": "/app/app/run_node.py",
    "health_server.py": "/app/app/health_server.py",
    "nautilus_actors.py": "/app/app/nautilus_actors.py",
    "approved_intent_client.py": "/app/data_client/approved_intent_client.py",
    "nautilus_config.py": "/app/persistence/nautilus_config.py",
    "persistence_init.py": "/app/persistence/__init__.py",
    "redis_namespace_lease.py": "/app/persistence/redis_namespace_lease.py",
    "redis_resp_client.py": "/app/persistence/redis_resp_client.py",
    "node_config.py": "/app/config/node_config.py",
    "risk_config.py": "/app/risk/config.py",
    "risk_init.py": "/app/risk/__init__.py",
    "routing_init.py": "/app/routing/__init__.py",
    "routing_multi_account.py": "/app/routing/multi_account.py",
    "binance_execution.py": BINANCE_DST,
    "binance_futures_execution.py": BINANCE_FUTURES_DST,
}
LEGACY_PATCH_MOUNTS = {
    key: value
    for key, value in EXPECTED_PATCH_MOUNTS.items()
    if key
    not in {
        "entry_batch.py", "account_execution_ledger.py", "execution_domain_init.py",
        "idempotency.py", "identifiers.py", "owned_order_recovery.py",
        "approved_intent_client.py",
        "bounded_task_worker.py",
        "control_plane_session.py",
        "health.py",
        "health_server.py",
        "intent_execution_inbox.py",
        "live_canary_execution.py",
        "node_config.py",
        "order_ownership.py",
        "projection_spool.py",
        "reconciliation.py",
        "nautilus_reconciliation_scope.py",
        "redis_safety.py",
        "risk_config.py",
        "risk_init.py",
        "run_node.py",
        "nautilus_config.py",
        "persistence_init.py",
        "redis_namespace_lease.py",
        "redis_resp_client.py",
        "routing_init.py",
        "routing_multi_account.py",
    }
}


class NodePatchMountContractTest(unittest.TestCase):
    def test_generator_mounts_match_container_bundle_contract(self):
        import make_container_bundle

        self.assertEqual(
            _generator_mounts(),
            {
                bundle_name: mount_target
                for bundle_name, _source, mount_target in (
                    make_container_bundle.BUNDLE_FILES
                )
            },
        )

    def test_hardening_generator_contains_full_transition_mount_list(self):
        self.assertEqual(_generator_mounts(), EXPECTED_PATCH_MOUNTS)
        deploy = HARDENING_DEPLOY.read_text(encoding="utf-8")
        self.assertIn("--require-transition-runtime", deploy)
        self.assertIn(
            'DELIVERY_MODE="${DELIVERY_MODE:-immutable_image}"',
            deploy,
        )
        self.assertIn('SKIP_RESUME="${SKIP_RESUME:-1}"', deploy)
        self.assertIn("build_immutable_node_image.py", deploy)
        self.assertIn('bash "$T/recreate-$node.sh"', deploy)
        self.assertIn('recreate_release_node "$node"', deploy)
        self.assertNotIn('for node in "${ALL_NODES[@]}"; do\n  bash', deploy)
        self.assertIn(
            'ROLLOUT_NODE="${ROLLOUT_NODE:-trader-v3-node-a}"',
            deploy,
        )
        self.assertIn("ACCOUNT_B_ROLLOUT_GATE", deploy)
        self.assertIn("ACCOUNT_B_EVIDENCE_FILE", deploy)
        self.assertIn('"soak_seconds", 0)) < 1800', deploy)
        self.assertIn('"verify_live_passed": True', deploy)
        self.assertIn('"canary_halted": True', deploy)
        self.assertIn("ACCOUNT_B_EVIDENCE_SIGNATURE", deploy)
        self.assertIn("write_account_b_reviewer_public_key", deploy)
        self.assertIn("-----BEGIN PUBLIC KEY-----", deploy)
        self.assertIn(
            "2b149fe2d7357dfea74441a1f6d6f1dd"
            "9ff6ea6a1a800fd5f2f7c2778339f928",
            deploy,
        )
        self.assertNotIn("ACCOUNT_B_EVIDENCE_PUBLIC_KEY", deploy)
        self.assertIn("verify_version_endpoint", deploy)
        self.assertNotIn('"type": "RESUME"', deploy)
        self.assertIn(
            "automatic RESUME is disabled; keep SKIP_RESUME=1",
            deploy,
        )
        self.assertIn(
            'NAUTILUS_INITIAL_TRADING_STATE=HALTED',
            GENERATOR.read_text(encoding="utf-8"),
        )
        self.assertIn("require_release_matches_bundle", deploy)
        self.assertRegex(
            deploy,
            r"risk_config\.py\|risk_init\.py\|projection_spool\.py",
        )
        self.assertIn(
            '--bundle-manifest "$STAGING/bundle-manifest.json"',
            deploy,
        )
        self.assertIn("verify-live", deploy)
        self.assertIn("staging missing host/read_api.py", deploy)
        self.assertIn("staging missing host/snapshot.py", deploy)
        self.assertIn(
            "staging missing host/decision_gateway/gateway.py",
            deploy,
        )
        self.assertIn(
            "staging missing host/hermes_signal_feeder.py",
            deploy,
        )
        self.assertIn("staging missing host/v3_trade.py", deploy)
        for required in (
            "staging missing host/execution_domain/entry_batch.py",
            'CHANGED_HOST+=("execution_domain_entry_batch")',
            "host__execution_domain_entry_batch.py",
            "post-install mismatch: host/execution_domain/entry_batch.py",
        ):
            self.assertIn(required, deploy)
        self.assertIn(
            "staging missing host/v3-trader/SKILL.md",
            deploy,
        )
        self.assertIn(
            "staging missing host/system_snapshot.v1.json",
            deploy,
        )
        self.assertIn(
            "staging missing host/execution_domain/control_plane.py",
            deploy,
        )
        self.assertIn(
            "staging missing host/execution_domain/portfolio_baseline.py",
            deploy,
        )
        self.assertIn(
            "staging missing host/execution_domain/order_ownership.py",
            deploy,
        )
        self.assertIn(
            "EXECUTION_DOMAIN_CONTROL_PLANE_TGT",
            deploy,
        )
        self.assertIn(
            "EXECUTION_DOMAIN_PORTFOLIO_BASELINE_TGT",
            deploy,
        )
        self.assertIn(
            "EXECUTION_DOMAIN_ORDER_OWNERSHIP_TGT",
            deploy,
        )
        self.assertIn(
            "host__execution_domain_portfolio_baseline.py",
            deploy,
        )
        self.assertIn(
            "host__execution_domain_order_ownership.py",
            deploy,
        )
        self.assertIn("DECISION_GATEWAY_TGT", deploy)
        self.assertIn("HERMES_FEEDER_TGT", deploy)
        self.assertIn("HERMES_V3_TRADE_TGT", deploy)
        self.assertIn("HERMES_V3_SKILL_TGT", deploy)
        self.assertIn(
            "/srv/hermes/profiles/trader/skills/trading/v3-trader",
            deploy,
        )
        self.assertIn("trader-v3-hermes-feeder", deploy)
        self.assertIn("hermes-gateway-trader", deploy)
        self.assertIn("host__decision_gateway.py", deploy)
        self.assertIn("host__hermes_signal_feeder.py", deploy)
        self.assertIn("host__v3_trade.py", deploy)
        self.assertIn("host__v3_trader_SKILL.md", deploy)
        self.assertIn("cat host/decision_gateway/gateway.py", deploy)
        self.assertIn("install_payload_atomically", deploy)
        self.assertIn("restart_hermes_units", deploy)
        self.assertNotIn("host/order_lifecycle_monitor.py", deploy)
        self.assertNotIn("host/trader-v3-trade-outcomes.service", deploy)

    def test_legacy_20260729_scripts_keep_their_historical_mount_contract(self):
        self.assertEqual(_shell_mounts(ROOT_WINDOW), LEGACY_PATCH_MOUNTS)
        self.assertEqual(_shell_mounts(ROOT_CONTINUE), LEGACY_PATCH_MOUNTS)

    def test_deploy_uses_mount_list_for_staging_backup_install_and_verification(self):
        text = ROOT_WINDOW.read_text(encoding="utf-8")

        self.assertIn('required+=("container/${mount_spec%%=*}")', text)
        self.assertIn('targets+=("$CP/${mount_spec%%=*}")', text)
        self.assertIn('"$D/container/$patch_filename"', text)
        self.assertIn('"$CP/$patch_filename"', text)
        self.assertGreaterEqual(text.count('"${NODE_PATCH_MOUNTS[@]}"'), 2)
        self.assertIn("deployment manifest lacks required mounts", text)

    def test_continuation_uses_mount_list_for_staging_hashes_and_verification(self):
        text = ROOT_CONTINUE.read_text(encoding="utf-8")

        self.assertIn('required_staging+=("container/${mount_spec%%=*}")', text)
        self.assertIn('f"container/{filename}"', text)
        self.assertIn('trader_root / "container-patches" / filename', text)
        self.assertGreaterEqual(text.count('"${NODE_PATCH_MOUNTS[@]}"'), 3)
        self.assertIn("deployment manifest lacks required mounts", text)


def _generator_mounts() -> dict[str, str]:
    namespace = runpy.run_path(str(GENERATOR))
    mounts = namespace["explicit_mounts"](
        Path("/srv/trader-v3"),
        "a",
        BINANCE_DST,
        BINANCE_FUTURES_DST,
    )
    result = {}
    for source, destination, mode in mounts:
        source_path = Path(source)
        if source_path.parent.name != "container-patches":
            continue
        if mode != "ro":
            raise AssertionError(f"patch mount is writable: {source} -> {destination}")
        result[source_path.name] = destination
    return result


def _shell_mounts(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"NODE_PATCH_MOUNTS=\(\n(?P<body>.*?)\n\)", text, re.DOTALL)
    if match is None:
        raise AssertionError(f"NODE_PATCH_MOUNTS missing from {path}")

    result = {}
    for raw_line in match.group("body").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not line.startswith('"') or not line.endswith('"'):
            raise AssertionError(f"invalid mount spec in {path}: {raw_line}")
        mount_spec = line[1:-1]
        mount_spec = mount_spec.replace("$BINANCE_EXEC_DST", BINANCE_DST)
        mount_spec = mount_spec.replace(
            "$BINANCE_FUTURES_EXEC_DST",
            BINANCE_FUTURES_DST,
        )
        filename, destination = mount_spec.split("=", 1)
        result[filename] = destination
    return result


if __name__ == "__main__":
    unittest.main()
