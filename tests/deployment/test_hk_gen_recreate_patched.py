import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "hk-gen-recreate-patched.py"
PATCH_FILES = (
    "intent_execution_planner.py",
    "contracts.py",
    "control_plane.py",
    "http_client.py",
    "projection_actor.py",
    "event_mapper.py",
    "intent_execution_strategy.py",
    "exchange_cancel_adapter.py",
    "lifecycle.py",
    "binance_adapter_config.py",
    "node.py",
    "nautilus_actors.py",
    "binance_execution.py",
    "binance_futures_execution.py",
)
BINANCE_DST = (
    "/usr/local/lib/python3.12/site-packages/"
    "nautilus_trader/adapters/binance/execution.py"
)
BINANCE_FUTURES_DST = (
    "/usr/local/lib/python3.12/site-packages/"
    "nautilus_trader/adapters/binance/futures/execution.py"
)


class GenRecreatePatchedTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.temp_path = Path(self.temp_dir.name)
        self.trader_root = self.temp_path / "trader-v3"
        self.patch_dir = self.trader_root / "container-patches"
        self.patch_dir.mkdir(parents=True)
        for filename in PATCH_FILES:
            (self.patch_dir / filename).write_text(filename, encoding="utf-8")

        self.fake_bin = self.temp_path / "bin"
        self.fake_bin.mkdir()
        self.docker_called = self.temp_path / "docker-called"
        fake_docker = self.fake_bin / "docker"
        fake_docker.write_text(
            "#!/bin/sh\n"
            'touch "$FAKE_DOCKER_CALLED"\n'
            'if [ "$1" = "inspect" ]; then\n'
            '  cat "$FAKE_DOCKER_INSPECT"\n'
            "  exit 0\n"
            "fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        fake_docker.chmod(0o755)

        self.inspect_path = self.temp_path / "inspect.json"
        inspect_payload = [
            {
                "Config": {
                    "Image": "trader-node:test",
                    "Env": [
                        "ACCOUNT_ID=account_a",
                        "NAUTILUS_INITIAL_TRADING_STATE=ACTIVE",
                        "PATH=/usr/bin",
                    ],
                    "Cmd": ["python", "-m", "app.run_node"],
                    "Entrypoint": None,
                },
                "NetworkSettings": {"Networks": {"trader-v3": {}}},
                "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
                "Mounts": [
                    {
                        "Source": str(self.patch_dir / "projection_actor.py"),
                        "Destination": "/app/wrong/actor.py",
                        "RW": False,
                    },
                    {
                        "Source": "/legacy/projection_actor.py",
                        "Destination": "/app/projection/actor.py",
                        "RW": False,
                    },
                    {
                        "Source": "/legacy/lifecycle.py.fixed",
                        "Destination": "/app/runtime/lifecycle.py",
                        "RW": False,
                    },
                    {
                        "Source": "/legacy/nautilus_actors.py.fixed",
                        "Destination": "/app/app/nautilus_actors.py",
                        "RW": False,
                    },
                    {
                        "Source": "/legacy/binance_adapter_config.py.fixed",
                        "Destination": "/app/runtime/binance_adapter_config.py",
                        "RW": False,
                    },
                    {
                        "Source": str(self.trader_root / "config" / "node-a.json"),
                        "Destination": "/cfg.json",
                        "RW": False,
                    },
                ],
            }
        ]
        self.inspect_path.write_text(json.dumps(inspect_payload), encoding="utf-8")

        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.fake_bin}:{self.env['PATH']}"
        self.env["TRADER_ROOT"] = str(self.trader_root)
        self.env["FAKE_DOCKER_CALLED"] = str(self.docker_called)
        self.env["FAKE_DOCKER_INSPECT"] = str(self.inspect_path)

    def run_script(self, *args):
        return subprocess.run(
            ["python3", str(SCRIPT), *args],
            cwd=REPO_ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

    def generated_mounts(self):
        recreate = self.trader_root / "recreate-trader-v3-node-a.sh"
        command = recreate.read_text(encoding="utf-8").splitlines()[-1]
        tokens = shlex.split(command)
        mounts = []
        for index, token in enumerate(tokens):
            if token == "-v":
                mounts.append(tokens[index + 1])
        return mounts

    def generated_environment(self):
        recreate = self.trader_root / "recreate-trader-v3-node-a.sh"
        command = recreate.read_text(encoding="utf-8").splitlines()[-1]
        tokens = shlex.split(command)
        environment = []
        for index, token in enumerate(tokens):
            if token == "-e":
                environment.append(tokens[index + 1])
        return environment

    def test_missing_binance_destination_fails_before_docker_inspect(self):
        result = self.run_script("trader-v3-node-a")

        self.assertEqual(result.returncode, 2)
        self.assertIn("FATAL: BINANCE_EXEC_DST", result.stderr)
        self.assertFalse(self.docker_called.exists())
        self.assertFalse(
            (self.trader_root / "recreate-trader-v3-node-a.sh").exists()
        )

    def test_generated_mounts_use_each_canonical_source_destination_pair_once(self):
        result = self.run_script(
            "trader-v3-node-a",
            BINANCE_DST,
            BINANCE_FUTURES_DST,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        mounts = self.generated_mounts()
        expected = {
            f"{self.patch_dir}/intent_execution_planner.py:"
            "/app/strategy/intent_execution_planner.py:ro",
            f"{self.patch_dir}/contracts.py:/app/execution_domain/contracts.py:ro",
            f"{self.patch_dir}/control_plane.py:"
            "/app/execution_domain/control_plane.py:ro",
            f"{self.patch_dir}/http_client.py:"
            "/app/execution_domain/http_client.py:ro",
            f"{self.patch_dir}/projection_actor.py:/app/projection/actor.py:ro",
            f"{self.patch_dir}/event_mapper.py:/app/projection/event_mapper.py:ro",
            f"{self.patch_dir}/intent_execution_strategy.py:"
            "/app/strategy/intent_execution_strategy.py:ro",
            f"{self.patch_dir}/exchange_cancel_adapter.py:"
            "/app/runtime/exchange_cancel_adapter.py:ro",
            f"{self.patch_dir}/lifecycle.py:/app/runtime/lifecycle.py:ro",
            f"{self.patch_dir}/binance_adapter_config.py:"
            "/app/runtime/binance_adapter_config.py:ro",
            f"{self.patch_dir}/node.py:/app/app/node.py:ro",
            f"{self.patch_dir}/nautilus_actors.py:"
            "/app/app/nautilus_actors.py:ro",
            f"{self.patch_dir}/binance_execution.py:{BINANCE_DST}:ro",
            f"{self.patch_dir}/binance_futures_execution.py:"
            f"{BINANCE_FUTURES_DST}:ro",
        }
        self.assertTrue(expected.issubset(set(mounts)))
        for mount in expected:
            self.assertEqual(mounts.count(mount), 1)
        self.assertNotIn(
            f"{self.patch_dir}/projection_actor.py:/app/wrong/actor.py:ro",
            mounts,
        )
        self.assertNotIn(
            "/legacy/projection_actor.py:/app/projection/actor.py:ro",
            mounts,
        )
        self.assertNotIn(
            "/legacy/lifecycle.py.fixed:/app/runtime/lifecycle.py:ro",
            mounts,
        )
        self.assertNotIn(
            "/legacy/nautilus_actors.py.fixed:/app/app/nautilus_actors.py:ro",
            mounts,
        )
        self.assertNotIn(
            "/legacy/binance_adapter_config.py.fixed:"
            "/app/runtime/binance_adapter_config.py:ro",
            mounts,
        )

    def test_missing_lifecycle_patch_fails_before_docker_inspect(self):
        (self.patch_dir / "lifecycle.py").unlink()

        result = self.run_script(
            "trader-v3-node-a",
            BINANCE_DST,
            BINANCE_FUTURES_DST,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("mount source is missing", result.stderr)
        self.assertIn("lifecycle.py", result.stderr)
        self.assertFalse(self.docker_called.exists())

    def test_generated_container_always_starts_halted(self):
        result = self.run_script(
            "trader-v3-node-a",
            BINANCE_DST,
            BINANCE_FUTURES_DST,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        environment = self.generated_environment()
        self.assertEqual(
            environment.count("NAUTILUS_INITIAL_TRADING_STATE=HALTED"),
            1,
        )
        self.assertNotIn("NAUTILUS_INITIAL_TRADING_STATE=ACTIVE", environment)

    def test_binance_destination_cannot_replace_another_patch(self):
        result = self.run_script(
            "trader-v3-node-a",
            "/app/projection/actor.py",
            BINANCE_FUTURES_DST,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("FATAL: duplicate mount destination", result.stderr)
        self.assertFalse(self.docker_called.exists())


if __name__ == "__main__":
    unittest.main()
