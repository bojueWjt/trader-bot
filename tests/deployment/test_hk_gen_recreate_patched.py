import json
import os
from pathlib import Path
import runpy
import shlex
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "hk-gen-recreate-patched.py"
BUNDLE_SCRIPT = REPO_ROOT / "scripts" / "make_container_bundle.py"
GENERATOR = runpy.run_path(str(SCRIPT))
PATCH_MOUNT_TARGETS = tuple(GENERATOR["PATCH_MOUNT_TARGETS"])
PATCH_FILES = tuple(item[0] for item in PATCH_MOUNT_TARGETS) + (
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
            (self.patch_dir / filename).write_text(
                filename,
                encoding="utf-8",
            )

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
                        "PATH=/custom/bin",
                        "NODE_STATE_DIR=/legacy-state",
                    ],
                    "Cmd": ["python", "-m", "app.run_node"],
                    "Entrypoint": None,
                    "User": "1000:1000",
                    "WorkingDir": "/app",
                    "Hostname": "node-a",
                },
                "NetworkSettings": {
                    "Networks": {
                        "trader-v3": {
                            "Aliases": [
                                "trader-v3-node-a",
                                "node-a",
                            ]
                        },
                        "metrics": {"Aliases": ["account-a-metrics"]},
                    }
                },
                "HostConfig": {
                    "RestartPolicy": {"Name": "unless-stopped"},
                    "PortBindings": {
                        "8081/tcp": [
                            {
                                "HostIp": "127.0.0.1",
                                "HostPort": "8081",
                            }
                        ]
                    },
                },
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
                        "Source": str(
                            self.trader_root / "config" / "node-a.json"
                        ),
                        "Destination": "/cfg.json",
                        "RW": False,
                    },
                ],
            }
        ]
        self.inspect_path.write_text(
            json.dumps(inspect_payload),
            encoding="utf-8",
        )

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

    def generated_lines(self):
        recreate = self.trader_root / "recreate-trader-v3-node-a.sh"
        return recreate.read_text(encoding="utf-8").splitlines()

    def generated_run_tokens(self):
        line = next(
            item for item in self.generated_lines()
            if item.startswith("docker run ")
        )
        return shlex.split(line)

    def generated_mounts(self):
        tokens = self.generated_run_tokens()
        mounts = []
        for index, token in enumerate(tokens):
            if token == "-v":
                mounts.append(tokens[index + 1])
        return mounts

    def generated_environment(self):
        tokens = self.generated_run_tokens()
        environment = []
        for index, token in enumerate(tokens):
            if token == "-e":
                environment.append(tokens[index + 1])
        return environment

    def option_value(self, option):
        tokens = self.generated_run_tokens()
        return tokens[tokens.index(option) + 1]

    def test_bundle_and_recreate_mount_contracts_are_exact(self):
        bundle_namespace = runpy.run_path(str(BUNDLE_SCRIPT))
        bundle_mounts = {
            (item[0], item[2])
            for item in bundle_namespace["BUNDLE_FILES"]
            if item[0] not in {
                "binance_execution.py",
                "binance_futures_execution.py",
            }
        }

        self.assertEqual(bundle_mounts, set(PATCH_MOUNT_TARGETS))

    def test_missing_binance_destination_fails_before_docker_inspect(self):
        result = self.run_script("trader-v3-node-a")

        self.assertEqual(result.returncode, 2)
        self.assertIn("FATAL: BINANCE_EXEC_DST", result.stderr)
        self.assertFalse(self.docker_called.exists())
        self.assertFalse(
            (self.trader_root / "recreate-trader-v3-node-a.sh").exists()
        )

    def test_generated_mounts_use_full_dependency_closed_mapping_once(self):
        result = self.run_script(
            "trader-v3-node-a",
            BINANCE_DST,
            BINANCE_FUTURES_DST,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        mounts = self.generated_mounts()
        expected = {
            f"{self.patch_dir}/{filename}:{destination}:ro"
            for filename, destination in PATCH_MOUNT_TARGETS
        }
        expected.update(
            {
                f"{self.patch_dir}/binance_execution.py:{BINANCE_DST}:ro",
                (
                    f"{self.patch_dir}/binance_futures_execution.py:"
                    f"{BINANCE_FUTURES_DST}:ro"
                ),
            }
        )
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
        self.assertIn(
            f"{self.trader_root}/config/node-a.json:/cfg.json:ro",
            mounts,
        )

    def test_missing_new_dependency_fails_before_docker_inspect(self):
        (self.patch_dir / "control_plane_session.py").unlink()

        result = self.run_script(
            "trader-v3-node-a",
            BINANCE_DST,
            BINANCE_FUTURES_DST,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("mount source is missing", result.stderr)
        self.assertIn("control_plane_session.py", result.stderr)
        self.assertFalse(self.docker_called.exists())

    def test_generated_container_is_halted_and_resource_bounded(self):
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
        self.assertNotIn(
            "NAUTILUS_INITIAL_TRADING_STATE=ACTIVE",
            environment,
        )
        self.assertEqual(environment.count("NODE_STATE_DIR=/state"), 1)
        self.assertNotIn("NODE_STATE_DIR=/legacy-state", environment)
        self.assertIn("PATH=/custom/bin", environment)
        self.assertEqual(self.option_value("--memory"), "768m")
        self.assertEqual(self.option_value("--memory-swap"), "768m")

    def test_generated_container_preserves_ports_networks_and_runtime_shape(self):
        result = self.run_script(
            "trader-v3-node-a",
            BINANCE_DST,
            BINANCE_FUTURES_DST,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        tokens = self.generated_run_tokens()
        self.assertIn("--network=trader-v3", tokens)
        self.assertIn("--network-alias", tokens)
        self.assertIn("node-a", tokens)
        self.assertEqual(
            self.option_value("--publish"),
            "127.0.0.1:8081:8081/tcp",
        )
        self.assertEqual(self.option_value("--user"), "1000:1000")
        self.assertEqual(self.option_value("--workdir"), "/app")
        self.assertEqual(self.option_value("--hostname"), "node-a")
        self.assertIn(
            "docker network connect --alias account-a-metrics "
            "metrics trader-v3-node-a",
            self.generated_lines(),
        )

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
