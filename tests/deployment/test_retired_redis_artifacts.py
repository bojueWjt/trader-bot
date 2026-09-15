from pathlib import Path
import hashlib
import importlib.util
import tempfile
import unittest
import json
import subprocess
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts/verify_retired_redis_artifacts.py"
spec = importlib.util.spec_from_file_location("retired_redis", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RetiredRedisArtifactsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve() / "volume"
        self.root.mkdir()
        self.rdb = self.root / "dump.rdb"
        self.rdb.write_bytes(b"verified historical bytes")
        self.backup = {
            "source_data_root": str(self.root),
            "source_volume_name": "retired-volume",
            "files": [{
                "path": "data/dump.rdb", "size_bytes": self.rdb.stat().st_size,
                "sha256": hashlib.sha256(self.rdb.read_bytes()).hexdigest(),
            }],
        }
        self.volume = {"Name": "retired-volume", "Mountpoint": str(self.root), "Driver": "local", "Options": None}

    def verify(self, containers=()):
        return module.verify_retained_files(self.backup, self.volume, containers)

    def test_matching_unmounted_retained_volume(self):
        self.assertEqual(self.verify(), self.root)

    def test_modified_bytes(self):
        self.rdb.write_bytes(b"modified historical bytes")
        with self.assertRaisesRegex(ValueError, "bytes differ"):
            self.verify()

    def test_extra_file(self):
        (self.root / "extra.rdb").write_bytes(b"unexpected")
        with self.assertRaisesRegex(ValueError, "file set"):
            self.verify()

    def test_missing_file(self):
        self.rdb.unlink()
        with self.assertRaisesRegex(ValueError, "file set"):
            self.verify()

    def test_source_attached_even_to_stopped_container(self):
        for source in [self.root, self.root.parent, self.rdb, self.root / "subdir"]:
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "attached"):
                self.verify([{"State": {"Running": False}, "Mounts": [{"Source": str(source)}]}])

    def test_volume_identity(self):
        for key, value in [("Name", "other"), ("Mountpoint", "/other"), ("Driver", "nfs"), ("Options", {"device": "/other"})]:
            with self.subTest(key=key):
                original = self.volume[key]
                self.volume[key] = value
                with self.assertRaises(ValueError):
                    self.verify()
                self.volume[key] = original

    def test_symlink_is_rejected(self):
        other = self.root.parent / "other.rdb"
        self.rdb.rename(other)
        self.rdb.symlink_to(other)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            self.verify()

    def test_traversal_and_duplicate_manifest_paths(self):
        item = self.backup["files"][0]
        for path in ["/dump.rdb", "data/../dump.rdb", "other/dump.rdb"]:
            item["path"] = path
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.verify()
        item["path"] = "data/dump.rdb"
        self.backup["files"].append(dict(item))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.verify()

    def test_checker_timeout_cleans_daemon_container(self):
        error = subprocess.TimeoutExpired("docker", 45)
        with patch.object(module, "run", side_effect=[error, "checker-id\n", "checker-id\n", ""]) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                module.check_rdb(self.root, "sha256:image", self.rdb)
        self.assertIn(unittest.mock.call("docker", "rm", "-f", "checker-id"), run.call_args_list)
        first = run.call_args_list[0].args
        self.assertIn("--network", first)
        self.assertIn("none", first)
        self.assertIn("--read-only", first)
        self.assertEqual(first[first.index("--user") + 1], f"{self.rdb.stat().st_uid}:{self.rdb.stat().st_gid}")

    def test_checker_failure_cleans_daemon_container(self):
        with patch.object(module, "run", side_effect=[RuntimeError("checker failed"), "checker-id\n", "", ""]):
            with self.assertRaisesRegex(RuntimeError, "checker failed"):
                module.check_rdb(self.root, "sha256:image", self.rdb)

    def test_checker_cleanup_failure_blocks_success(self):
        with patch.object(module, "run", side_effect=["valid", "checker-id\n", "", "checker-id\n"]):
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                module.check_rdb(self.root, "sha256:image", self.rdb)

    def prepare_main(self):
        self.backup.update({"source_container_id": "old-id", "source_image_digest": "sha256:old", "source_aof_enabled": 0, "source_appendonly": "no", "source_rdb_filename": "dump.rdb"})
        self.backup_path = self.root.parent / "backup.json"
        self.backup_path.write_text(json.dumps(self.backup))
        capacity = {"source_backup_manifest_sha256": module.digest(self.backup_path), "active_volume": "live", "legacy_container": "retired"}
        self.capacity_path = self.root.parent / "capacity.json"
        self.capacity_path.write_text(json.dumps(capacity))
        return ["verify", str(self.backup_path), str(self.capacity_path), "maintenance_fence"]

    def test_main_rejects_other_modes_before_docker(self):
        args = self.prepare_main()
        args[-1] = "migration_rebaseline_stopped"
        with patch.object(module.sys, "argv", args), patch.object(module, "run") as run:
            with self.assertRaisesRegex(ValueError, "limited to maintenance"):
                module.main()
            run.assert_not_called()

    def test_main_rejects_remaining_original_container(self):
        args = self.prepare_main()
        for container in [{"Name": "/retired", "Id": "other"}, {"Name": "/renamed", "Id": "old-id"}]:
            with patch.object(module.sys, "argv", args), patch.object(module, "inspect_containers", return_value=[container]):
                with self.assertRaisesRegex(ValueError, "container.*exists"):
                    module.main()

    def test_main_rejects_wrong_image(self):
        args = self.prepare_main()
        with patch.object(module.sys, "argv", args), patch.object(module, "inspect_containers", return_value=[]), patch.object(module, "run", side_effect=[json.dumps([self.volume]), json.dumps([{"Id": "wrong"}])]):
            with self.assertRaisesRegex(ValueError, "image differs"):
                module.main()

    def test_main_rechecks_new_mount_after_checker(self):
        args = self.prepare_main()
        mounted = [{"Mounts": [{"Source": str(self.rdb)}]}]
        with patch.object(module.sys, "argv", args), patch.object(module, "inspect_containers", side_effect=[[], mounted]), patch.object(module, "run", side_effect=[json.dumps([self.volume]), json.dumps([{"Id": "sha256:old"}]), json.dumps([self.volume])]), patch.object(module, "check_rdb", return_value="valid"):
            with self.assertRaisesRegex(ValueError, "attached"):
                module.main()
