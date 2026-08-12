from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "redis_namespace_janitor.py"
ACCOUNT_A_PERSISTENCE_NAMESPACE = (
    "trader-TRADER-ACCOUNT-A:00000000-0000-4000-8000-000000000001"
)
ACCOUNT_B_PERSISTENCE_NAMESPACE = (
    "trader-TRADER-ACCOUNT-B:00000000-0000-4000-8000-000000000002"
)
ACCOUNT_C_PERSISTENCE_NAMESPACE = (
    "trader-TRADER-ACCOUNT-C:00000000-0000-4000-8000-000000000003"
)
ACCOUNT_D_PERSISTENCE_NAMESPACE = (
    "trader-TRADER-ACCOUNT-D:00000000-0000-4000-8000-000000000004"
)


def _load_script():
    spec = importlib.util.spec_from_file_location("redis_namespace_janitor", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeRedis:
    def __init__(
        self,
        keys: Iterable[str],
        *,
        idle_seconds: dict[str, int | None],
        active_registry: Iterable[str] = (),
        server_time: int = 1000,
        run_id: str = "current-run-id",
        uptime_in_seconds: int = 50,
    ) -> None:
        self.keys = {key.encode("utf-8") for key in keys}
        self.idle_seconds = idle_seconds
        self.active_registry = {
            namespace.encode("utf-8") for namespace in active_registry
        }
        self.registry_scores = {
            namespace: server_time for namespace in active_registry
        }
        self.lease_records: dict[str, dict[str, object]] = {}
        self.server_time = server_time
        self.run_id = run_id
        self.uptime_in_seconds = uptime_in_seconds
        self.unlinked: list[bytes] = []
        self.unlink_calls: list[tuple[bytes, ...]] = []
        self.eval_calls: list[tuple[bytes, ...]] = []
        self.before_eval: Callable[[], None] | None = None
        self.after_eval: Callable[[], None] | None = None
        self.maxmemory = 2 * 1024 * 1024 * 1024
        self.used_memory = 64 * 1024 * 1024
        self.dbsize_calls = 0
        self.memory_info_calls = 0

    def scan_iter(self, match: str, count: int):
        del count
        prefix = match.removesuffix("*").encode("utf-8")
        return iter(sorted(key for key in self.keys if key.startswith(prefix)))

    def object(self, subcommand: str, key: bytes):
        assert subcommand == "idletime"
        return self.idle_seconds.get(key.decode("utf-8"))

    def zrangebyscore(self, key: str, minimum: int, maximum: str):
        del key, maximum
        return sorted(
            namespace
            for namespace in self.active_registry
            if self.registry_scores.get(
                namespace.decode("utf-8"),
                self.server_time,
            )
            >= minimum
        )

    def time(self):
        return self.server_time, 0

    def info(self, section: str):
        if section == "server":
            return {
                "run_id": self.run_id,
                "uptime_in_seconds": self.uptime_in_seconds,
            }
        assert section == "memory"
        self.memory_info_calls += 1
        return {
            "used_memory": self.used_memory,
            "maxmemory": self.maxmemory,
        }

    def dbsize(self) -> int:
        self.dbsize_calls += 1
        return len(self.keys)

    def hget(self, key: str, field: str):
        assert key.endswith(":leases")
        record = self.lease_records.get(field)
        if record is None:
            return None
        return json.dumps(record, sort_keys=True)

    def zscore(self, key: str, member: str):
        del key
        score = self.registry_scores.get(member)
        if score is None:
            return None
        return float(score)

    def eval(self, script: str, numkeys: int, *keys_and_args: object):
        assert "redis-namespace-janitor:atomic-unlink-v2" in script
        assert numkeys >= 3
        if self.before_eval is not None:
            before_eval = self.before_eval
            self.before_eval = None
            before_eval()

        registry_key = keys_and_args[0]
        del registry_key
        metadata_key = str(keys_and_args[1])
        assert metadata_key.endswith(":leases")
        keys = tuple(keys_and_args[2:numkeys])
        namespace = str(keys_and_args[numkeys])
        fresh_after_epoch = int(keys_and_args[numkeys + 1])
        min_idle_seconds = int(keys_and_args[numkeys + 2])
        lease_namespace_count = int(keys_and_args[numkeys + 3])
        lease_namespaces = tuple(
            str(item)
            for item in keys_and_args[
                numkeys + 4 : numkeys + 4 + lease_namespace_count
            ]
        )
        namespace_bytes = namespace.encode("utf-8")

        namespace_status = self._persistence_namespace_status(
            namespace,
            namespace_bytes=namespace_bytes,
            fresh_after_epoch=fresh_after_epoch,
            lease_namespaces=lease_namespaces,
        )
        if namespace_status is not None:
            return [0, namespace_status]

        namespace_prefix = f"{namespace}:".encode()
        for key in keys:
            assert isinstance(key, bytes)
            if not key.startswith(namespace_prefix):
                return [0, b"WRONG_NAMESPACE"]
            idle_seconds = self.object("idletime", key)
            if idle_seconds is None:
                return [0, b"KEY_MISSING"]
            if int(idle_seconds) < min_idle_seconds:
                return [0, b"KEY_ACTIVE"]

        namespace_status = self._persistence_namespace_status(
            namespace,
            namespace_bytes=namespace_bytes,
            fresh_after_epoch=fresh_after_epoch,
            lease_namespaces=lease_namespaces,
        )
        if namespace_status is not None:
            return [0, namespace_status]

        self.eval_calls.append(keys)
        deleted = self._unlink(*keys)
        if self.after_eval is not None:
            self.after_eval()
        return [deleted, b"DELETED"]

    def _persistence_namespace_status(
        self,
        namespace: str,
        *,
        namespace_bytes: bytes,
        fresh_after_epoch: int,
        lease_namespaces: tuple[str, ...],
    ) -> bytes | None:
        direct_score = self.registry_scores.get(namespace)
        if (
            namespace_bytes in self.active_registry
            and direct_score is None
        ):
            direct_score = self.server_time
        if (
            direct_score is not None
            and direct_score >= fresh_after_epoch
        ):
            return b"ACTIVE"

        for lease_namespace in lease_namespaces:
            lease_score = self.registry_scores.get(lease_namespace)
            if lease_score is None or lease_score < fresh_after_epoch:
                continue
            record = self.lease_records.get(lease_namespace)
            if record is None:
                return b"LEASE_METADATA_MISSING"
            record_lease_namespace = record.get("lease_namespace")
            if record_lease_namespace is None:
                record_lease_namespace = record.get("namespace")
            fencing_token = record.get("fencing_token")
            if isinstance(fencing_token, str) and fencing_token.isdigit():
                fencing_token = int(fencing_token)
            persistence_instance_id = record.get("persistence_instance_id")
            persistence_namespace = record.get("persistence_namespace")
            if (
                record_lease_namespace != lease_namespace
                or isinstance(fencing_token, bool)
                or not isinstance(fencing_token, int)
                or fencing_token < 1
                or record.get("refreshed_at_epoch") != lease_score
                or not isinstance(persistence_instance_id, str)
                or module_uuid4_pattern().fullmatch(
                    persistence_instance_id
                )
                is None
                or persistence_namespace
                != f"{lease_namespace}:{persistence_instance_id}"
            ):
                return b"LEASE_METADATA_INVALID"
            if persistence_namespace == namespace:
                return b"ACTIVE"
        return None

    def _unlink(self, *keys: bytes):
        self.unlink_calls.append(keys)
        self.unlinked.extend(keys)
        self.keys.difference_update(keys)
        return len(keys)


class FakeClock:
    def __init__(self) -> None:
        self.current = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += seconds


def module_uuid4_pattern():
    import re

    return re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
        r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )


def _legacy_key(trader: str, runtime_id: str, suffix: str) -> str:
    return f"trader-{trader}:{runtime_id}:nautilus:account-a:{suffix}"


def _write_safety_manifest(
    tmp_path: Path,
    redis: FakeRedis,
    *,
    backup_completed_at_epoch: int = 940,
    reconciliation_completed_at_epoch: int = 990,
    persistence_namespaces: dict[str, str] | None = None,
) -> dict[str, object]:
    backup = tmp_path / "dump.rdb"
    backup.write_bytes(b"verified cold redis backup")
    artifact_sha256 = hashlib.sha256(backup.read_bytes()).hexdigest()
    if persistence_namespaces is None:
        persistence_namespaces = {
            "account-a": ACCOUNT_A_PERSISTENCE_NAMESPACE,
            "account-b": ACCOUNT_B_PERSISTENCE_NAMESPACE,
            "account-c": ACCOUNT_C_PERSISTENCE_NAMESPACE,
            "account-d": ACCOUNT_D_PERSISTENCE_NAMESPACE,
        }
    nodes = []
    for index, account_id in enumerate(
        ("account-a", "account-b", "account-c", "account-d"),
        start=1,
    ):
        lease_namespace = f"trader-TRADER-{account_id.upper()}"
        persistence_namespace = persistence_namespaces[account_id]
        release_id = f"release-{account_id[-1]}"
        refreshed_at_epoch = redis.server_time - 5
        redis.active_registry.add(lease_namespace.encode("utf-8"))
        redis.registry_scores[lease_namespace] = refreshed_at_epoch
        redis.lease_records[lease_namespace] = {
            "namespace": lease_namespace,
            "persistence_instance_id": persistence_namespace.rsplit(
                ":",
                1,
            )[1],
            "persistence_namespace": persistence_namespace,
            "owner": f"owner-{account_id[-1]}",
            "release_id": release_id,
            "fencing_token": index,
            "refreshed_at_epoch": refreshed_at_epoch,
        }
        nodes.append(
            {
                "account_id": account_id,
                "lease_namespace": lease_namespace,
                "persistence_namespace": persistence_namespace,
                "owner": f"owner-{account_id[-1]}",
                "release_id": release_id,
                "fencing_token": index,
                "reconciliation": {
                    "state": "HEALTHY",
                    "completed_at_epoch": reconciliation_completed_at_epoch,
                    "release_id": release_id,
                },
            }
        )
    return {
        "schema_version": "trader-redis-janitor-safety/v2",
        "backup": {
            "mode": "cold",
            "source_run_id": "stopped-source-run-id",
            "completed_at_epoch": backup_completed_at_epoch,
            "artifacts": [
                {
                    "kind": "rdb",
                    "path": str(backup.resolve()),
                    "sha256": artifact_sha256,
                }
            ],
        },
        "nodes": nodes,
    }


def test_expected_stable_namespaces_cover_exactly_accounts_a_to_d() -> None:
    module = _load_script()

    assert module.EXPECTED_STABLE_NAMESPACES == {
        "account-a": "trader-TRADER-ACCOUNT-A",
        "account-b": "trader-TRADER-ACCOUNT-B",
        "account-c": "trader-TRADER-ACCOUNT-C",
        "account-d": "trader-TRADER-ACCOUNT-D",
    }
    assert len(set(module.EXPECTED_STABLE_NAMESPACES.values())) == 4


def _write_legacy_safety_manifest(
    tmp_path: Path,
    redis: FakeRedis,
) -> dict[str, object]:
    manifest = _write_safety_manifest(tmp_path, redis)
    manifest["schema_version"] = "trader-redis-janitor-safety/v1"
    nodes = manifest["nodes"]
    assert isinstance(nodes, list)
    for node in nodes:
        assert isinstance(node, dict)
        node["namespace"] = node.pop("lease_namespace")
        node.pop("persistence_namespace")
    return manifest


def test_v2_manifest_validates_stable_lease_and_protects_active_generation(
    tmp_path: Path,
) -> None:
    module = _load_script()
    stale_id = "22222222-2222-4222-8222-222222222222"
    prefix = "trader-TRADER-ACCOUNT-A:"
    stale_namespace = f"{prefix}{stale_id}"
    active_key = (
        f"{ACCOUNT_A_PERSISTENCE_NAMESPACE}:nautilus:account-a:stream:active"
    )
    stale_key = _legacy_key(
        "TRADER-ACCOUNT-A",
        stale_id,
        "stream:stale",
    )
    redis = FakeRedis(
        [active_key, stale_key],
        idle_seconds={
            active_key: 7200,
            stale_key: 7200,
        },
    )
    manifest = _write_safety_manifest(tmp_path, redis)

    report = module.run_janitor(
        redis,
        legacy_prefixes=[prefix],
        active_namespaces=set(),
        registry_key="namespace-registry",
        registry_fresh_after_epoch=1,
        min_idle_seconds=3600,
        max_namespaces=10,
        max_keys=10,
        apply=True,
        safety_manifest=manifest,
    )

    assert report.protected_namespaces == (
        ACCOUNT_A_PERSISTENCE_NAMESPACE,
    )
    assert report.selected_namespaces == (stale_namespace,)
    assert report.deleted_keys == 1
    assert active_key.encode("utf-8") in redis.keys
    assert stale_key.encode("utf-8") not in redis.keys


def test_janitor_defaults_to_dry_run_and_protects_registered_namespace() -> None:
    module = _load_script()
    active_id = "11111111-1111-4111-8111-111111111111"
    stale_id = "22222222-2222-4222-8222-222222222222"
    prefix = "trader-TRADER-ACCOUNT-A:"
    active_namespace = f"{prefix}{active_id}"
    stale_namespace = f"{prefix}{stale_id}"
    keys = [
        _legacy_key("TRADER-ACCOUNT-A", active_id, "stream:active"),
        _legacy_key("TRADER-ACCOUNT-A", stale_id, "stream:stale-a"),
        _legacy_key("TRADER-ACCOUNT-A", stale_id, "stream:stale-b"),
    ]
    redis = FakeRedis(
        keys,
        idle_seconds={key: 7200 for key in keys},
        active_registry=[active_namespace],
    )

    report = module.run_janitor(
        redis,
        legacy_prefixes=[prefix],
        active_namespaces=set(),
        registry_key="namespace-registry",
        registry_fresh_after_epoch=1,
        min_idle_seconds=3600,
        max_namespaces=10,
        max_keys=10,
        apply=False,
    )

    assert report.candidate_namespaces == (stale_namespace,)
    assert report.protected_namespaces == (active_namespace,)
    assert report.selected_keys == 2
    assert report.deleted_keys == 0
    assert redis.unlinked == []


def test_dry_run_resolves_active_generation_from_lease_metadata(
    tmp_path: Path,
) -> None:
    module = _load_script()
    prefix = "trader-TRADER-ACCOUNT-A:"
    active_key = (
        f"{ACCOUNT_A_PERSISTENCE_NAMESPACE}:nautilus:account-a:stream:active"
    )
    redis = FakeRedis(
        [active_key],
        idle_seconds={active_key: 7200},
    )
    _write_safety_manifest(tmp_path, redis)

    report = module.run_janitor(
        redis,
        legacy_prefixes=[prefix],
        active_namespaces=set(),
        registry_key="namespace-registry",
        registry_fresh_after_epoch=1,
        min_idle_seconds=3600,
        max_namespaces=10,
        max_keys=10,
        apply=False,
    )

    assert report.protected_namespaces == (
        ACCOUNT_A_PERSISTENCE_NAMESPACE,
    )
    assert report.candidate_namespaces == ()
    assert report.selected_namespaces == ()


def test_dry_run_derives_generation_from_legacy_lease_metadata(
    tmp_path: Path,
) -> None:
    module = _load_script()
    prefix = "trader-TRADER-ACCOUNT-A:"
    active_key = (
        f"{ACCOUNT_A_PERSISTENCE_NAMESPACE}:nautilus:account-a:stream:active"
    )
    lease_namespace = "trader-TRADER-ACCOUNT-A"
    redis = FakeRedis(
        [active_key],
        idle_seconds={active_key: 7200},
    )
    _write_safety_manifest(tmp_path, redis)
    del redis.lease_records[lease_namespace]["persistence_instance_id"]
    del redis.lease_records[lease_namespace]["persistence_namespace"]

    report = module.run_janitor(
        redis,
        legacy_prefixes=[prefix],
        active_namespaces=set(),
        registry_key="namespace-registry",
        registry_fresh_after_epoch=1,
        min_idle_seconds=3600,
        max_namespaces=10,
        max_keys=10,
        apply=False,
    )

    assert report.protected_namespaces == ()
    assert report.candidate_namespaces == (
        ACCOUNT_A_PERSISTENCE_NAMESPACE,
    )
    assert report.deleted_keys == 0


def test_janitor_requires_safety_manifest_before_apply() -> None:
    module = _load_script()
    runtime_id = "22222222-2222-4222-8222-222222222222"
    prefix = "trader-TRADER-ACCOUNT-A:"
    key = _legacy_key("TRADER-ACCOUNT-A", runtime_id, "stream:stale")
    redis = FakeRedis([key], idle_seconds={key: 7200})

    try:
        module.run_janitor(
            redis,
            legacy_prefixes=[prefix],
            active_namespaces=set(),
            registry_key="namespace-registry",
            registry_fresh_after_epoch=1,
            min_idle_seconds=3600,
            max_namespaces=10,
            max_keys=10,
            apply=True,
            safety_manifest=None,
        )
    except module.JanitorSafetyError as exc:
        assert "safety manifest" in str(exc)
    else:
        raise AssertionError("apply must require a verified safety manifest")


def test_legacy_v1_manifest_remains_available_for_dry_run(
    tmp_path: Path,
) -> None:
    module = _load_script()
    runtime_id = "22222222-2222-4222-8222-222222222222"
    prefix = "trader-TRADER-ACCOUNT-A:"
    key = _legacy_key("TRADER-ACCOUNT-A", runtime_id, "stream:stale")
    redis = FakeRedis([key], idle_seconds={key: 7200})
    manifest = _write_legacy_safety_manifest(tmp_path, redis)

    report = module.run_janitor(
        redis,
        legacy_prefixes=[prefix],
        active_namespaces=set(),
        registry_key="namespace-registry",
        registry_fresh_after_epoch=1,
        min_idle_seconds=3600,
        max_namespaces=10,
        max_keys=10,
        apply=False,
        safety_manifest=manifest,
    )

    assert report.apply is False
    assert report.safety_manifest_verified is False
    assert report.deleted_keys == 0


def test_legacy_v1_manifest_fails_closed_for_apply(tmp_path: Path) -> None:
    module = _load_script()
    runtime_id = "22222222-2222-4222-8222-222222222222"
    prefix = "trader-TRADER-ACCOUNT-A:"
    key = _legacy_key("TRADER-ACCOUNT-A", runtime_id, "stream:stale")
    redis = FakeRedis([key], idle_seconds={key: 7200})
    manifest = _write_legacy_safety_manifest(tmp_path, redis)

    with pytest.raises(
        module.JanitorSafetyError,
        match="apply requires.*v2",
    ):
        module.run_janitor(
            redis,
            legacy_prefixes=[prefix],
            active_namespaces=set(),
            registry_key="namespace-registry",
            registry_fresh_after_epoch=1,
            min_idle_seconds=3600,
            max_namespaces=10,
            max_keys=10,
            apply=True,
            safety_manifest=manifest,
        )

    assert redis.eval_calls == []
    assert redis.unlinked == []


def test_v2_apply_rejects_incomplete_legacy_lease_metadata(
    tmp_path: Path,
) -> None:
    module = _load_script()
    runtime_id = "22222222-2222-4222-8222-222222222222"
    prefix = "trader-TRADER-ACCOUNT-A:"
    key = _legacy_key("TRADER-ACCOUNT-A", runtime_id, "stream:stale")
    lease_namespace = "trader-TRADER-ACCOUNT-A"
    redis = FakeRedis([key], idle_seconds={key: 7200})
    manifest = _write_safety_manifest(tmp_path, redis)
    del redis.lease_records[lease_namespace]["persistence_instance_id"]
    del redis.lease_records[lease_namespace]["persistence_namespace"]

    with pytest.raises(
        module.JanitorSafetyError,
        match="generation metadata is incomplete",
    ):
        module.run_janitor(
            redis,
            legacy_prefixes=[prefix],
            active_namespaces=set(),
            registry_key="namespace-registry",
            registry_fresh_after_epoch=1,
            min_idle_seconds=3600,
            max_namespaces=10,
            max_keys=10,
            apply=True,
            safety_manifest=manifest,
        )

    assert redis.eval_calls == []
    assert redis.unlinked == []
    assert key.encode("utf-8") in redis.keys


@pytest.mark.parametrize(
    ("account_id", "node_index"),
    (
        ("account-a", 0),
        ("account-b", 1),
        ("account-c", 2),
        ("account-d", 3),
    ),
)
def test_v2_manifest_requires_uuid4_persistence_namespace(
    tmp_path: Path,
    account_id: str,
    node_index: int,
) -> None:
    module = _load_script()
    prefix = "trader-TRADER-ACCOUNT-A:"
    redis = FakeRedis([], idle_seconds={})
    manifest = _write_safety_manifest(tmp_path, redis)
    lease_namespace = f"trader-TRADER-{account_id.upper()}"
    invalid_persistence_namespace = (
        f"{lease_namespace}:00000000-0000-1000-8000-000000000001"
    )
    node = manifest["nodes"][node_index]
    assert isinstance(node, dict)
    node["persistence_namespace"] = invalid_persistence_namespace
    redis.lease_records[lease_namespace][
        "persistence_namespace"
    ] = invalid_persistence_namespace

    with pytest.raises(module.JanitorSafetyError, match="UUID4"):
        module.run_janitor(
            redis,
            legacy_prefixes=[prefix],
            active_namespaces=set(),
            registry_key="namespace-registry",
            registry_fresh_after_epoch=1,
            min_idle_seconds=3600,
            max_namespaces=10,
            max_keys=10,
            apply=True,
            safety_manifest=manifest,
        )

    assert redis.unlinked == []


def test_v2_manifest_requires_distinct_a_to_d_lease_owners(
    tmp_path: Path,
) -> None:
    module = _load_script()
    prefix = "trader-TRADER-ACCOUNT-A:"
    redis = FakeRedis([], idle_seconds={})
    manifest = _write_safety_manifest(tmp_path, redis)
    nodes = manifest["nodes"]
    assert isinstance(nodes, list)
    account_d = nodes[3]
    assert isinstance(account_d, dict)
    account_d["owner"] = "owner-a"
    redis.lease_records["trader-TRADER-ACCOUNT-D"]["owner"] = "owner-a"

    with pytest.raises(
        module.JanitorSafetyError,
        match="A-D namespace lease owners must be distinct",
    ):
        module.run_janitor(
            redis,
            legacy_prefixes=[prefix],
            active_namespaces=set(),
            registry_key="namespace-registry",
            registry_fresh_after_epoch=1,
            min_idle_seconds=3600,
            max_namespaces=10,
            max_keys=10,
            apply=True,
            safety_manifest=manifest,
        )

    assert redis.unlinked == []


def test_janitor_rejects_manual_boolean_safety_bypass() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--redis-url",
            "redis://127.0.0.1:6379/0",
            "--legacy-prefix",
            "trader-TRADER-ACCOUNT-A:",
            "--apply",
            "--confirm-active-set-complete",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr


def test_janitor_rejects_tampered_cold_backup(tmp_path: Path) -> None:
    module = _load_script()
    runtime_id = "22222222-2222-4222-8222-222222222222"
    prefix = "trader-TRADER-ACCOUNT-A:"
    key = _legacy_key("TRADER-ACCOUNT-A", runtime_id, "stream:stale")
    redis = FakeRedis([key], idle_seconds={key: 7200})
    manifest = _write_safety_manifest(tmp_path, redis)
    backup = manifest["backup"]
    artifact = backup["artifacts"][0]
    Path(artifact["path"]).write_bytes(b"tampered")

    with pytest.raises(module.JanitorSafetyError, match="sha256"):
        module.run_janitor(
            redis,
            legacy_prefixes=[prefix],
            active_namespaces=set(),
            registry_key="namespace-registry",
            registry_fresh_after_epoch=1,
            min_idle_seconds=3600,
            max_namespaces=10,
            max_keys=10,
            apply=True,
            safety_manifest=manifest,
        )

    assert redis.unlinked == []


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda manifest, redis: manifest["backup"].update(
                {"source_run_id": redis.run_id}
            ),
            "run_id",
        ),
        (
            lambda manifest, redis: manifest["nodes"].pop(),
            "account-a, account-b, account-c, and account-d",
        ),
        (
            lambda manifest, redis: manifest["nodes"][0]["reconciliation"].update(
                {"completed_at_epoch": redis.server_time - 301}
            ),
            "reconciliation",
        ),
        (
            lambda manifest, redis: redis.lease_records[
                "trader-TRADER-ACCOUNT-A"
            ].update({"owner": "replacement-owner"}),
            "lease",
        ),
    ],
)
def test_janitor_rejects_invalid_runtime_safety_evidence(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    module = _load_script()
    runtime_id = "22222222-2222-4222-8222-222222222222"
    prefix = "trader-TRADER-ACCOUNT-A:"
    key = _legacy_key("TRADER-ACCOUNT-A", runtime_id, "stream:stale")
    redis = FakeRedis([key], idle_seconds={key: 7200})
    manifest = _write_safety_manifest(tmp_path, redis)
    mutation(manifest, redis)

    with pytest.raises(module.JanitorSafetyError, match=message):
        module.run_janitor(
            redis,
            legacy_prefixes=[prefix],
            active_namespaces=set(),
            registry_key="namespace-registry",
            registry_fresh_after_epoch=1,
            min_idle_seconds=3600,
            max_namespaces=10,
            max_keys=10,
            apply=True,
            safety_manifest=manifest,
        )

    assert redis.unlinked == []


def test_janitor_skips_recent_or_unknown_keys_and_honors_batch_limits(
    tmp_path: Path,
) -> None:
    module = _load_script()
    first_id = "22222222-2222-4222-8222-222222222222"
    second_id = "33333333-3333-4333-8333-333333333333"
    recent_id = "44444444-4444-4444-8444-444444444444"
    prefix = "trader-TRADER-ACCOUNT-A:"
    first_keys = [
        _legacy_key("TRADER-ACCOUNT-A", first_id, "stream:a"),
        _legacy_key("TRADER-ACCOUNT-A", first_id, "stream:b"),
    ]
    second_key = _legacy_key("TRADER-ACCOUNT-A", second_id, "stream:c")
    recent_key = _legacy_key("TRADER-ACCOUNT-A", recent_id, "stream:d")
    redis = FakeRedis(
        [*first_keys, second_key, recent_key],
        idle_seconds={
            first_keys[0]: 7200,
            first_keys[1]: 7200,
            second_key: 7200,
            recent_key: 12,
        },
        active_registry=["trader-TRADER-ACCOUNT-A:stable"],
    )
    clock = FakeClock()
    manifest = _write_safety_manifest(tmp_path, redis)

    report = module.run_janitor(
        redis,
        legacy_prefixes=[prefix],
        active_namespaces=set(),
        registry_key="namespace-registry",
        registry_fresh_after_epoch=1,
        min_idle_seconds=3600,
        max_namespaces=1,
        max_keys=2,
        apply=True,
        safety_manifest=manifest,
        unlink_batch_size=1,
        max_unlink_keys_per_second=2,
        sleep_fn=clock.sleep,
        monotonic_fn=clock.monotonic,
    )

    assert report.selected_namespaces == (f"{prefix}{first_id}",)
    assert report.selected_keys == 2
    assert report.deleted_keys == 2
    assert set(redis.unlinked) == {key.encode("utf-8") for key in first_keys}
    assert len(redis.unlink_calls) == 2
    assert report.post_batch_verifications == 2
    assert redis.dbsize_calls == 3
    assert redis.memory_info_calls == 2
    assert clock.sleeps == []
    assert recent_key.encode("utf-8") in redis.keys


def test_janitor_stops_after_post_batch_key_count_drift(
    tmp_path: Path,
) -> None:
    module = _load_script()
    runtime_id = "55555555-5555-4555-8555-555555555555"
    prefix = "trader-TRADER-ACCOUNT-A:"
    keys = [
        _legacy_key("TRADER-ACCOUNT-A", runtime_id, "stream:a"),
        _legacy_key("TRADER-ACCOUNT-A", runtime_id, "stream:b"),
    ]
    redis = FakeRedis(keys, idle_seconds={key: 7200 for key in keys})
    manifest = _write_safety_manifest(tmp_path, redis)

    def add_untracked_key() -> None:
        redis.after_eval = None
        redis.keys.add(b"untracked:key")

    redis.after_eval = add_untracked_key

    with pytest.raises(module.JanitorSafetyError, match="key count"):
        module.run_janitor(
            redis,
            legacy_prefixes=[prefix],
            active_namespaces=set(),
            registry_key="namespace-registry",
            registry_fresh_after_epoch=1,
            min_idle_seconds=3600,
            max_namespaces=1,
            max_keys=2,
            apply=True,
            safety_manifest=manifest,
            unlink_batch_size=1,
            max_unlink_keys_per_second=100_000,
            sleep_fn=lambda _seconds: None,
        )

    assert len(redis.unlinked) == 1


def test_janitor_stops_after_post_batch_capacity_threshold(
    tmp_path: Path,
) -> None:
    module = _load_script()
    runtime_id = "66666666-6666-4666-8666-666666666666"
    prefix = "trader-TRADER-ACCOUNT-A:"
    key = _legacy_key("TRADER-ACCOUNT-A", runtime_id, "stream:a")
    redis = FakeRedis([key], idle_seconds={key: 7200})
    redis.used_memory = int(redis.maxmemory * 0.86)
    manifest = _write_safety_manifest(tmp_path, redis)

    with pytest.raises(module.JanitorSafetyError, match="85%"):
        module.run_janitor(
            redis,
            legacy_prefixes=[prefix],
            active_namespaces=set(),
            registry_key="namespace-registry",
            registry_fresh_after_epoch=1,
            min_idle_seconds=3600,
            max_namespaces=1,
            max_keys=1,
            apply=True,
            safety_manifest=manifest,
        )

    assert len(redis.unlinked) == 1


def test_janitor_stops_after_post_batch_reconciliation_drift(
    tmp_path: Path,
) -> None:
    module = _load_script()
    runtime_id = "77777777-7777-4777-8777-777777777777"
    prefix = "trader-TRADER-ACCOUNT-A:"
    key = _legacy_key("TRADER-ACCOUNT-A", runtime_id, "stream:a")
    redis = FakeRedis([key], idle_seconds={key: 7200})
    manifest = _write_safety_manifest(tmp_path, redis)

    def replace_owner() -> None:
        redis.after_eval = None
        redis.lease_records["trader-TRADER-ACCOUNT-A"]["owner"] = (
            "replacement-owner"
        )

    redis.after_eval = replace_owner

    with pytest.raises(module.JanitorSafetyError, match="lease identity"):
        module.run_janitor(
            redis,
            legacy_prefixes=[prefix],
            active_namespaces=set(),
            registry_key="namespace-registry",
            registry_fresh_after_epoch=1,
            min_idle_seconds=3600,
            max_namespaces=1,
            max_keys=1,
            apply=True,
            safety_manifest=manifest,
        )

    assert len(redis.unlinked) == 1


def test_janitor_partially_cleans_large_inactive_namespace_across_runs(
    tmp_path: Path,
) -> None:
    module = _load_script()
    runtime_id = "55555555-5555-4555-8555-555555555555"
    prefix = "trader-TRADER-ACCOUNT-A:"
    namespace = f"{prefix}{runtime_id}"
    keys = [
        _legacy_key("TRADER-ACCOUNT-A", runtime_id, f"stream:{index:04d}")
        for index in range(1500)
    ]
    redis = FakeRedis(
        keys,
        idle_seconds={key: 7200 for key in keys},
        active_registry=["trader-TRADER-ACCOUNT-A:stable"],
    )
    manifest = _write_safety_manifest(tmp_path, redis)

    first = module.run_janitor(
        redis,
        legacy_prefixes=[prefix],
        active_namespaces=set(),
        registry_key="namespace-registry",
        registry_fresh_after_epoch=1,
        min_idle_seconds=3600,
        max_namespaces=1,
        max_keys=1000,
        apply=True,
        safety_manifest=manifest,
        unlink_batch_size=100,
        max_unlink_keys_per_second=100_000,
        sleep_fn=lambda _seconds: None,
    )

    assert first.selected_namespaces == (namespace,)
    assert first.partial_namespaces == (namespace,)
    assert first.selected_keys == 1000
    assert first.deleted_keys == 1000
    assert len(redis.keys) == 500

    second = module.run_janitor(
        redis,
        legacy_prefixes=[prefix],
        active_namespaces=set(),
        registry_key="namespace-registry",
        registry_fresh_after_epoch=1,
        min_idle_seconds=3600,
        max_namespaces=1,
        max_keys=1000,
        apply=True,
        safety_manifest=manifest,
        unlink_batch_size=100,
        max_unlink_keys_per_second=100_000,
        sleep_fn=lambda _seconds: None,
    )

    assert second.selected_namespaces == (namespace,)
    assert second.partial_namespaces == ()
    assert second.selected_keys == 500
    assert second.deleted_keys == 500
    assert redis.keys == set()


def test_janitor_stops_when_namespace_reactivates_between_batches(
    tmp_path: Path,
) -> None:
    module = _load_script()
    runtime_id = "66666666-6666-4666-8666-666666666666"
    prefix = "trader-TRADER-ACCOUNT-A:"
    namespace = f"{prefix}{runtime_id}"
    keys = [
        _legacy_key("TRADER-ACCOUNT-A", runtime_id, "stream:a"),
        _legacy_key("TRADER-ACCOUNT-A", runtime_id, "stream:b"),
    ]

    class ReactivatingRedis(FakeRedis):
        def _unlink(self, *unlink_keys: bytes):
            deleted = super()._unlink(*unlink_keys)
            self.active_registry.add(namespace.encode("utf-8"))
            return deleted

    redis = ReactivatingRedis(
        keys,
        idle_seconds={key: 7200 for key in keys},
        active_registry=["trader-TRADER-ACCOUNT-A:stable"],
    )
    manifest = _write_safety_manifest(tmp_path, redis)

    try:
        module.run_janitor(
            redis,
            legacy_prefixes=[prefix],
            active_namespaces=set(),
            registry_key="namespace-registry",
            registry_fresh_after_epoch=1,
            min_idle_seconds=3600,
            max_namespaces=1,
            max_keys=2,
            apply=True,
            safety_manifest=manifest,
            unlink_batch_size=1,
            max_unlink_keys_per_second=100_000,
            sleep_fn=lambda _seconds: None,
        )
    except module.JanitorSafetyError as exc:
        assert "became active" in str(exc)
    else:
        raise AssertionError("reactivated namespace must stop deletion")

    assert len(redis.unlinked) == 1
    assert len(redis.keys) == 1


def test_janitor_atomic_script_blocks_registration_at_delete_boundary(
    tmp_path: Path,
) -> None:
    module = _load_script()
    runtime_id = "77777777-7777-4777-8777-777777777777"
    prefix = "trader-TRADER-ACCOUNT-A:"
    namespace = f"{prefix}{runtime_id}"
    key = _legacy_key("TRADER-ACCOUNT-A", runtime_id, "stream:stale")
    redis = FakeRedis([key], idle_seconds={key: 7200})
    manifest = _write_safety_manifest(tmp_path, redis)
    redis.before_eval = lambda: redis.active_registry.add(namespace.encode("utf-8"))

    try:
        module.run_janitor(
            redis,
            legacy_prefixes=[prefix],
            active_namespaces=set(),
            registry_key="namespace-registry",
            registry_fresh_after_epoch=1,
            min_idle_seconds=3600,
            max_namespaces=1,
            max_keys=1,
            apply=True,
            safety_manifest=manifest,
        )
    except module.JanitorSafetyError as exc:
        assert "became active" in str(exc)
    else:
        raise AssertionError("atomic delete must fail closed for a new lease")

    assert redis.unlinked == []
    assert key.encode("utf-8") in redis.keys


def test_atomic_unlink_blocks_generation_activated_by_stable_lease(
    tmp_path: Path,
) -> None:
    module = _load_script()
    runtime_id = "00000000-0000-4000-8000-000000000003"
    prefix = "trader-TRADER-ACCOUNT-A:"
    persistence_namespace = f"{prefix}{runtime_id}"
    key = _legacy_key("TRADER-ACCOUNT-A", runtime_id, "stream:stale")
    lease_namespace = "trader-TRADER-ACCOUNT-A"
    redis = FakeRedis([key], idle_seconds={key: 7200})
    manifest = _write_safety_manifest(tmp_path, redis)

    def activate_candidate_generation() -> None:
        record = redis.lease_records[lease_namespace]
        record.update(
            {
                "persistence_instance_id": runtime_id,
                "persistence_namespace": persistence_namespace,
                "owner": "replacement-owner",
                "release_id": "replacement-release",
                "fencing_token": 3,
                "refreshed_at_epoch": redis.server_time,
            }
        )
        redis.registry_scores[lease_namespace] = redis.server_time

    redis.before_eval = activate_candidate_generation

    with pytest.raises(module.JanitorSafetyError, match="became active"):
        module.run_janitor(
            redis,
            legacy_prefixes=[prefix],
            active_namespaces=set(),
            registry_key="namespace-registry",
            registry_fresh_after_epoch=1,
            min_idle_seconds=3600,
            max_namespaces=1,
            max_keys=1,
            apply=True,
            safety_manifest=manifest,
        )

    assert redis.unlinked == []
    assert key.encode("utf-8") in redis.keys


def test_atomic_unlink_fails_closed_for_invalid_generation_metadata(
    tmp_path: Path,
) -> None:
    module = _load_script()
    runtime_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    prefix = "trader-TRADER-ACCOUNT-A:"
    key = _legacy_key("TRADER-ACCOUNT-A", runtime_id, "stream:stale")
    lease_namespace = "trader-TRADER-ACCOUNT-A"
    redis = FakeRedis([key], idle_seconds={key: 7200})
    manifest = _write_safety_manifest(tmp_path, redis)

    def corrupt_active_generation_metadata() -> None:
        redis.lease_records[lease_namespace]["persistence_namespace"] = 42

    redis.before_eval = corrupt_active_generation_metadata

    with pytest.raises(
        module.JanitorSafetyError,
        match="active lease metadata changed",
    ):
        module.run_janitor(
            redis,
            legacy_prefixes=[prefix],
            active_namespaces=set(),
            registry_key="namespace-registry",
            registry_fresh_after_epoch=1,
            min_idle_seconds=3600,
            max_namespaces=1,
            max_keys=1,
            apply=True,
            safety_manifest=manifest,
        )

    assert redis.unlinked == []
    assert key.encode("utf-8") in redis.keys


def test_janitor_caps_atomic_batch_and_paces_using_monotonic_elapsed_time(
    tmp_path: Path,
) -> None:
    module = _load_script()
    runtime_id = "88888888-8888-4888-8888-888888888888"
    prefix = "trader-TRADER-ACCOUNT-A:"
    keys = [
        _legacy_key("TRADER-ACCOUNT-A", runtime_id, f"stream:{index:04d}")
        for index in range(250)
    ]
    redis = FakeRedis(keys, idle_seconds={key: 7200 for key in keys})
    clock = FakeClock()
    manifest = _write_safety_manifest(tmp_path, redis)

    report = module.run_janitor(
        redis,
        legacy_prefixes=[prefix],
        active_namespaces=set(),
        registry_key="namespace-registry",
        registry_fresh_after_epoch=1,
        min_idle_seconds=3600,
        max_namespaces=1,
        max_keys=250,
        apply=True,
        safety_manifest=manifest,
        unlink_batch_size=250,
        max_unlink_keys_per_second=100,
        sleep_fn=clock.sleep,
        monotonic_fn=clock.monotonic,
    )

    assert report.deleted_keys == 250
    assert [len(batch) for batch in redis.eval_calls] == [100, 100, 50]
    assert clock.sleeps == [1.0, 0.5]
