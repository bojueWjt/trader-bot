from pathlib import Path
import re
import runpy
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = REPO_ROOT / "scripts" / "hk-gen-recreate-patched.py"
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
    "intent_execution_planner.py": "/app/strategy/intent_execution_planner.py",
    "contracts.py": "/app/execution_domain/contracts.py",
    "control_plane.py": "/app/execution_domain/control_plane.py",
    "http_client.py": "/app/execution_domain/http_client.py",
    "projection_actor.py": "/app/projection/actor.py",
    "event_mapper.py": "/app/projection/event_mapper.py",
    "intent_execution_strategy.py": "/app/strategy/intent_execution_strategy.py",
    "exchange_cancel_adapter.py": "/app/runtime/exchange_cancel_adapter.py",
    "lifecycle.py": "/app/runtime/lifecycle.py",
    "binance_adapter_config.py": "/app/runtime/binance_adapter_config.py",
    "node.py": "/app/app/node.py",
    "nautilus_actors.py": "/app/app/nautilus_actors.py",
    "binance_execution.py": BINANCE_DST,
    "binance_futures_execution.py": BINANCE_FUTURES_DST,
}


class NodePatchMountContractTest(unittest.TestCase):
    def test_generator_deploy_and_continuation_share_full_patch_mount_list(self):
        self.assertEqual(_generator_mounts(), EXPECTED_PATCH_MOUNTS)
        self.assertEqual(_shell_mounts(ROOT_WINDOW), EXPECTED_PATCH_MOUNTS)
        self.assertEqual(_shell_mounts(ROOT_CONTINUE), EXPECTED_PATCH_MOUNTS)

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
