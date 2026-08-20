from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_SCRIPT = REPO_ROOT / "scripts" / "redis_namespace_registry.py"
LEASE_MODULE = (
    REPO_ROOT
    / "services"
    / "nautilus-node"
    / "persistence"
    / "redis_namespace_lease.py"
)
_REDIS_FENCING_EPOCH = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _load_lease_module():
    spec = importlib.util.spec_from_file_location(
        "redis_namespace_lease",
        LEASE_MODULE,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeLeaseRedis:
    def __init__(self) -> None:
        self.fencing_counter = 0
        self.server_time = 1000
        self.redis_fencing_epoch = _REDIS_FENCING_EPOCH
        self.scores: dict[str, int] = {}
        self.records: dict[str, dict[str, object]] = {}

    def eval(self, script: str, numkeys: int, *keys_and_args: object):
        del numkeys
        if "redis-namespace-lease:acquire-v6" in script:
            return self._acquire(*keys_and_args)
        if "redis-namespace-lease:acquire-v4" in script:
            return self._acquire(*keys_and_args)
        if "redis-namespace-lease:refresh-v4" in script:
            return self._refresh(*keys_and_args)
        if "redis-namespace-lease:remove-v4" in script:
            return self._remove(*keys_and_args)
        if "redis-namespace-lease:force-remove-stale-v2" in script:
            return self._force_remove_stale(*keys_and_args)
        raise AssertionError("unknown lease script")

    def time(self):
        return (self.server_time, 0)

    def zrangebyscore(
        self,
        key: str,
        minimum: int,
        maximum: str,
        *,
        withscores: bool,
    ):
        del key, maximum
        result = []
        for namespace, score in sorted(self.scores.items()):
            if score >= minimum:
                if withscores:
                    result.append((namespace, float(score)))
                else:
                    result.append(namespace)
        return result

    def hget(self, key: str, namespace: str):
        del key
        record = self.records.get(namespace)
        if record is None:
            return None
        return json.dumps(record, sort_keys=True)

    def zscore(self, key: str, namespace: str):
        del key
        score = self.scores.get(namespace)
        if score is None:
            return None
        return float(score)

    def _acquire(self, *values: object):
        (
            _registry,
            _metadata,
            _counter,
            _epoch_key,
            namespace,
            owner,
            release_id,
            max_age_seconds,
            candidate_instance_id,
        ) = values
        namespace = str(namespace)
        owner = str(owner)
        release_id = str(release_id)
        max_age_seconds = int(max_age_seconds)
        instance_id = str(candidate_instance_id)
        persistence_namespace = f"{namespace}:{instance_id}"
        now = self.server_time
        fresh_after = now - max_age_seconds
        if not _is_canonical_uuid4(self.redis_fencing_epoch):
            return [0, 0, "CORRUPT_EPOCH"]
        current = self.records.get(namespace)
        if current is not None:
            current_token = _record_fencing_token(current)
            if current_token is False:
                return [0, 0, "CORRUPT"]
            if current.get("redis_fencing_epoch") != self.redis_fencing_epoch:
                return [0, current_token, "EPOCH_MISMATCH"]
            if not _record_metadata_valid(namespace, current):
                return [0, 0, "CORRUPT"]
            current_identity = _record_persistence_identity(
                namespace,
                current,
            )
            if current_identity is False:
                return [0, 0, "CORRUPT"]
            current_instance_id, current_persistence_namespace = (
                current_identity
            )
            current_score = self.scores.get(namespace)
            current_refreshed_at = int(current["refreshed_at_epoch"])
            if current_score != current_refreshed_at:
                return [0, current_token, "CORRUPT"]
            same_owner = current["owner"] == owner
            same_release = current["release_id"] == release_id
            current_is_fresh = current_refreshed_at >= fresh_after
            if same_owner and same_release and current_is_fresh:
                current["fencing_token"] = _persisted_fencing_token(
                    current_token
                )
                current["persistence_instance_id"] = current_instance_id
                current["persistence_namespace"] = (
                    current_persistence_namespace
                )
                current["refreshed_at_epoch"] = now
                self.scores[namespace] = now
                return [
                    1,
                    current_token,
                    now,
                    current_instance_id,
                    current_persistence_namespace,
                    self.redis_fencing_epoch,
                ]
            if current_is_fresh:
                return [0, current_token, "HELD"]
            if not _owner_matches_node_identity(current["owner"], owner):
                return [0, current_token, "STALE_FOREIGN"]
        elif self.scores.get(namespace, 0) >= fresh_after:
            return [0, 0, "HELD_LEGACY"]
        if current is not None:
            self.fencing_counter = max(
                self.fencing_counter,
                current_token,
            )
        if self.fencing_counter >= 0xFFFFFFFFFFFF:
            return [0, 0, "OVERFLOW"]
        self.fencing_counter += 1
        self.records[namespace] = {
            "namespace": namespace,
            "owner": owner,
            "release_id": release_id,
            "redis_fencing_epoch": self.redis_fencing_epoch,
            "fencing_token": _persisted_fencing_token(
                self.fencing_counter
            ),
            "persistence_instance_id": instance_id,
            "persistence_namespace": persistence_namespace,
            "refreshed_at_epoch": now,
        }
        self.scores[namespace] = now
        return [
            1,
            self.fencing_counter,
            now,
            instance_id,
            persistence_namespace,
            self.redis_fencing_epoch,
        ]

    def _refresh(self, *values: object):
        (
            _registry,
            _metadata,
            _epoch_key,
            namespace,
            owner,
            release_id,
            token,
            max_age_seconds,
            expected_redis_fencing_epoch,
        ) = values
        namespace = str(namespace)
        now = self.server_time
        if not _is_canonical_uuid4(self.redis_fencing_epoch):
            return [0, "CORRUPT_EPOCH", now]
        if str(expected_redis_fencing_epoch) != self.redis_fencing_epoch:
            return [0, "EPOCH_CHANGED", now]
        current = self.records.get(namespace)
        if current is None:
            return [0, "MISSING", now]
        current_token = _record_fencing_token(current)
        if current_token is False:
            return [0, "CORRUPT", now]
        if current.get("redis_fencing_epoch") != self.redis_fencing_epoch:
            return [0, "EPOCH_MISMATCH", now]
        if not _record_metadata_valid(namespace, current):
            return [0, "CORRUPT", now]
        current_identity = _record_persistence_identity(
            namespace,
            current,
        )
        if current_identity is False:
            return [0, "CORRUPT", now]
        expected_instance_id, expected_namespace = current_identity
        expected = (
            current["owner"] == str(owner)
            and current["release_id"] == str(release_id)
            and current_token == int(token)
        )
        if not expected:
            return [0, "FENCED", now]
        current_score = self.scores.get(namespace)
        current_refreshed_at = int(current["refreshed_at_epoch"])
        if current_score != current_refreshed_at:
            return [0, "CORRUPT", now]
        if current_refreshed_at < now - int(max_age_seconds):
            return [0, "EXPIRED", now]
        current["persistence_instance_id"] = expected_instance_id
        current["persistence_namespace"] = expected_namespace
        current["fencing_token"] = _persisted_fencing_token(int(token))
        current["refreshed_at_epoch"] = now
        self.scores[namespace] = now
        return [
            1,
            "REFRESHED",
            now,
            expected_instance_id,
            expected_namespace,
            self.redis_fencing_epoch,
        ]

    def _remove(self, *values: object):
        (
            _registry,
            _metadata,
            _epoch_key,
            namespace,
            owner,
            release_id,
            token,
            expected_redis_fencing_epoch,
        ) = values
        namespace = str(namespace)
        if not _is_canonical_uuid4(self.redis_fencing_epoch):
            return [0, "CORRUPT_EPOCH"]
        if str(expected_redis_fencing_epoch) != self.redis_fencing_epoch:
            return [0, "EPOCH_CHANGED"]
        current = self.records.get(namespace)
        if current is None:
            return [0, "MISSING_IDENTITY"]
        current_token = _record_fencing_token(current)
        if current_token is False:
            return [0, "CORRUPT"]
        if current.get("redis_fencing_epoch") != self.redis_fencing_epoch:
            return [0, "EPOCH_MISMATCH"]
        if not _record_metadata_valid(namespace, current):
            return [0, "CORRUPT"]
        current_identity = _record_persistence_identity(
            namespace,
            current,
        )
        if current_identity is False:
            return [0, "CORRUPT"]
        expected = (
            current["owner"] == str(owner)
            and current["release_id"] == str(release_id)
            and current_token == int(token)
        )
        if not expected:
            return [0, "FENCED"]
        del self.records[namespace]
        self.scores.pop(namespace, None)
        return [1, "REMOVED"]

    def _force_remove_stale(self, *values: object):
        (
            _registry,
            _metadata,
            _epoch_key,
            namespace,
            max_age_seconds,
        ) = values
        namespace = str(namespace)
        max_age_seconds = int(max_age_seconds)
        if not _is_canonical_uuid4(self.redis_fencing_epoch):
            return [0, "CORRUPT_EPOCH", self.server_time]
        current = self.records.get(namespace)
        score = self.scores.get(namespace)
        if current is not None:
            current_token = _record_fencing_token(current)
            if current_token is False:
                return [0, "CORRUPT", self.server_time]
            if current.get("redis_fencing_epoch") != self.redis_fencing_epoch:
                return [0, "EPOCH_MISMATCH", self.server_time]
            if not _record_metadata_valid(namespace, current):
                return [0, "CORRUPT", self.server_time]
            if _record_persistence_identity(namespace, current) is False:
                return [0, "CORRUPT", self.server_time]
            refreshed_at = int(current["refreshed_at_epoch"])
            if score != refreshed_at:
                return [0, "CORRUPT", self.server_time]
            if refreshed_at >= self.server_time - max_age_seconds:
                return [0, "FRESH", self.server_time]
            del self.records[namespace]
            self.scores.pop(namespace, None)
            return [1, "REMOVED_STALE", self.server_time]
        if score is None:
            return [0, "MISSING", self.server_time]
        if score >= self.server_time - max_age_seconds:
            return [0, "FRESH_LEGACY", self.server_time]
        self.scores.pop(namespace, None)
        return [1, "REMOVED_STALE_LEGACY", self.server_time]


def test_fake_acquire_corrupt_metadata_matches_lua_raw_contract() -> None:
    redis = FakeLeaseRedis()
    namespace = "trader-TRADER-ACCOUNT-A"
    redis.records[namespace] = {
        "namespace": "trader-TRADER-ACCOUNT-B",
        "owner": "node-a",
        "release_id": "release-a",
        "redis_fencing_epoch": _REDIS_FENCING_EPOCH,
        "fencing_token": 7,
        "persistence_instance_id": _UUID4_A,
        "persistence_namespace": f"{namespace}:{_UUID4_A}",
        "refreshed_at_epoch": 1000,
    }
    redis.scores[namespace] = 1000

    result = redis._acquire(
        "registry",
        "metadata",
        "counter",
        "epoch",
        namespace,
        "node-b",
        "release-b",
        300,
        _UUID4_B,
    )

    assert result == [0, 0, "CORRUPT"]


def test_lease_acquire_refresh_list_and_release() -> None:
    module = _load_lease_module()
    redis = FakeLeaseRedis()
    lease = module.RedisNamespaceLease(
        redis,
        namespace="trader-TRADER-ACCOUNT-A",
        owner="node-a",
        release_id="release-abc",
        persistence_instance_id_factory=lambda: _UUID4_A,
    )

    acquired = lease.acquire()
    redis.server_time = 1050
    refreshed = lease.refresh()
    active = module.list_active_namespace_leases(
        redis,
        fresh_after_epoch=1001,
    )
    persisted_record = dict(redis.records[acquired.namespace])
    released = lease.release()

    assert acquired.fencing_token == 1
    assert acquired.namespace == "trader-TRADER-ACCOUNT-A"
    assert acquired.persistence_instance_id == _UUID4_A
    assert acquired.persistence_namespace == (
        f"trader-TRADER-ACCOUNT-A:{_UUID4_A}"
    )
    assert persisted_record["persistence_instance_id"] == (
        acquired.persistence_instance_id
    )
    assert persisted_record["persistence_namespace"] == (
        acquired.persistence_namespace
    )
    assert refreshed.fencing_token == 1
    assert refreshed.persistence_instance_id == acquired.persistence_instance_id
    assert refreshed.persistence_namespace == acquired.persistence_namespace
    assert refreshed.refreshed_at_epoch == 1050
    assert active == (refreshed,)
    assert released is True
    assert module.list_active_namespace_leases(
        redis,
        fresh_after_epoch=1,
    ) == ()


def test_repeat_acquire_by_same_identity_preserves_fencing_token() -> None:
    module = _load_lease_module()
    redis = FakeLeaseRedis()
    first = module.RedisNamespaceLease(
        redis,
        namespace="trader-TRADER-ACCOUNT-A",
        owner="node-a",
        release_id="release-abc",
        persistence_instance_id_factory=lambda: _UUID4_A,
    ).acquire()
    del redis.records[first.namespace]["persistence_instance_id"]
    del redis.records[first.namespace]["persistence_namespace"]
    redis.server_time = 1010
    second = module.RedisNamespaceLease(
        redis,
        namespace="trader-TRADER-ACCOUNT-A",
        owner="node-a",
        release_id="release-abc",
        persistence_instance_id_factory=lambda: _UUID4_B,
    ).acquire()

    assert first.fencing_token == second.fencing_token
    assert second.persistence_instance_id == _legacy_instance_id(1)
    assert redis.records[first.namespace]["persistence_namespace"] == (
        second.persistence_namespace
    )
    assert second.refreshed_at_epoch == 1010


def test_takeover_uses_a_distinct_persistence_generation() -> None:
    module = _load_lease_module()
    redis = FakeLeaseRedis()
    first = module.RedisNamespaceLease(
        redis,
        namespace="trader-TRADER-ACCOUNT-A",
        owner="node-a",
        release_id="release-old",
        persistence_instance_id_factory=lambda: _UUID4_A,
    ).acquire()
    redis.server_time = 1400
    replacement = module.RedisNamespaceLease(
        redis,
        namespace="trader-TRADER-ACCOUNT-A",
        owner="node-a",
        release_id="release-new",
        persistence_instance_id_factory=lambda: _UUID4_B,
    ).acquire()

    assert first.namespace == replacement.namespace
    assert first.persistence_instance_id != replacement.persistence_instance_id
    assert first.persistence_namespace != replacement.persistence_namespace
    assert replacement.persistence_instance_id == _UUID4_B


def test_get_and_list_derive_generation_for_legacy_metadata() -> None:
    module = _load_lease_module()
    redis = FakeLeaseRedis()
    namespace = "trader-TRADER-ACCOUNT-A"
    redis.records[namespace] = {
        "namespace": namespace,
        "owner": "node-a",
        "release_id": "release-a",
        "redis_fencing_epoch": _REDIS_FENCING_EPOCH,
        "fencing_token": 7,
        "refreshed_at_epoch": 1000,
    }
    redis.scores[namespace] = 1000

    current = module.get_namespace_lease(redis, namespace)
    active = module.list_active_namespace_leases(
        redis,
        fresh_after_epoch=900,
    )

    assert current is not False
    assert current.persistence_instance_id == (
        "00000000-0000-4000-8000-000000000007"
    )
    assert current.persistence_namespace == (
        "trader-TRADER-ACCOUNT-A:"
        "00000000-0000-4000-8000-000000000007"
    )
    assert active == (current,)


def test_refresh_backfills_generation_for_legacy_metadata() -> None:
    module = _load_lease_module()
    redis = FakeLeaseRedis()
    namespace = "trader-TRADER-ACCOUNT-A"
    redis.records[namespace] = {
        "namespace": namespace,
        "owner": "node-a",
        "release_id": "release-a",
        "redis_fencing_epoch": _REDIS_FENCING_EPOCH,
        "fencing_token": 7,
        "refreshed_at_epoch": 1000,
    }
    redis.scores[namespace] = 1000
    legacy_record = module.get_namespace_lease(redis, namespace)
    assert legacy_record is not False

    refreshed = module.refresh_namespace_lease(
        redis,
        record=legacy_record,
        refreshed_at_epoch=1050,
    )

    assert refreshed.persistence_instance_id == (
        "00000000-0000-4000-8000-000000000007"
    )
    assert redis.records[namespace]["persistence_instance_id"] == (
        refreshed.persistence_instance_id
    )
    assert redis.records[namespace]["persistence_namespace"] == (
        refreshed.persistence_namespace
    )


def test_repeat_acquire_backfills_persistence_namespace_only_metadata() -> None:
    module = _load_lease_module()
    redis = FakeLeaseRedis()
    namespace = "trader-TRADER-ACCOUNT-A"
    redis.records[namespace] = {
        "namespace": namespace,
        "owner": "node-a",
        "release_id": "release-a",
        "redis_fencing_epoch": _REDIS_FENCING_EPOCH,
        "fencing_token": 7,
        "persistence_namespace": f"{namespace}:{_UUID4_A}",
        "refreshed_at_epoch": 1000,
    }
    redis.scores[namespace] = 1000

    acquired = module.RedisNamespaceLease(
        redis,
        namespace=namespace,
        owner="node-a",
        release_id="release-a",
        persistence_instance_id_factory=lambda: _UUID4_B,
    ).acquire()

    assert acquired.fencing_token == 7
    assert acquired.persistence_instance_id == _UUID4_A
    assert redis.records[namespace]["persistence_instance_id"] == _UUID4_A


def test_explicit_null_generation_fails_closed() -> None:
    module = _load_lease_module()
    redis = FakeLeaseRedis()
    namespace = "trader-TRADER-ACCOUNT-A"
    redis.records[namespace] = {
        "namespace": namespace,
        "owner": "node-a",
        "release_id": "release-a",
        "redis_fencing_epoch": _REDIS_FENCING_EPOCH,
        "fencing_token": 7,
        "persistence_instance_id": None,
        "persistence_namespace": None,
        "refreshed_at_epoch": 1000,
    }
    redis.scores[namespace] = 1000

    with pytest.raises(module.RedisNamespaceLeaseLost, match="corrupt"):
        module.RedisNamespaceLease(
            redis,
            namespace=namespace,
            owner="node-a",
            release_id="release-a",
            persistence_instance_id_factory=lambda: _UUID4_A,
        ).acquire()


def test_explicit_false_generation_fails_closed_for_all_mutations() -> None:
    module = _load_lease_module()
    redis = FakeLeaseRedis()
    lease = module.RedisNamespaceLease(
        redis,
        namespace="trader-TRADER-ACCOUNT-A",
        owner="node-a",
        release_id="release-a",
        persistence_instance_id_factory=lambda: _UUID4_A,
    )
    record = lease.acquire()
    redis.records[record.namespace]["persistence_instance_id"] = False
    redis.records[record.namespace]["persistence_namespace"] = False

    with pytest.raises(module.RedisNamespaceLeaseLost, match="corrupt"):
        module.RedisNamespaceLease(
            redis,
            namespace=record.namespace,
            owner=record.owner,
            release_id=record.release_id,
            persistence_instance_id_factory=lambda: _UUID4_B,
        ).acquire()
    with pytest.raises(module.RedisNamespaceLeaseLost, match="corrupt"):
        lease.refresh()

    assert lease.release() is False
    with pytest.raises(module.RedisNamespaceLeaseError, match="corrupt"):
        module.force_remove_stale_namespace_lease(
            redis,
            record.namespace,
        )
    assert record.namespace in redis.records
    assert record.namespace in redis.scores


def test_get_fails_closed_for_persistence_namespace_mismatch() -> None:
    module = _load_lease_module()
    redis = FakeLeaseRedis()
    namespace = "trader-TRADER-ACCOUNT-A"
    redis.records[namespace] = {
        "namespace": namespace,
        "owner": "node-a",
        "release_id": "release-a",
        "redis_fencing_epoch": _REDIS_FENCING_EPOCH,
        "fencing_token": 7,
        "persistence_instance_id": _UUID4_A,
        "persistence_namespace": (
            f"trader-TRADER-ACCOUNT-A:{_UUID4_B}"
        ),
        "refreshed_at_epoch": 1000,
    }

    try:
        module.get_namespace_lease(redis, namespace)
    except module.RedisNamespaceLeaseError as exc:
        assert "metadata is invalid" in str(exc).lower()
    else:
        raise AssertionError("mismatched generation metadata must fail closed")


def test_refresh_fails_closed_when_stored_generation_changes() -> None:
    module = _load_lease_module()
    redis = FakeLeaseRedis()
    lease = module.RedisNamespaceLease(
        redis,
        namespace="trader-TRADER-ACCOUNT-A",
        owner="node-a",
        release_id="release-a",
        persistence_instance_id_factory=lambda: _UUID4_A,
    )
    record = lease.acquire()
    redis.records[record.namespace]["persistence_namespace"] = (
        f"trader-TRADER-ACCOUNT-A:{_UUID4_B}"
    )

    try:
        lease.refresh()
    except module.RedisNamespaceLeaseLost as exc:
        assert "corrupt" in str(exc).lower()
    else:
        raise AssertionError("changed generation metadata must fence refresh")


def test_acquire_fails_closed_when_generation_counter_overflows() -> None:
    module = _load_lease_module()
    redis = FakeLeaseRedis()
    redis.fencing_counter = 0xFFFFFFFFFFFF
    lease = module.RedisNamespaceLease(
        redis,
        namespace="trader-TRADER-ACCOUNT-A",
        owner="node-a",
        release_id="release-a",
        persistence_instance_id_factory=lambda: _UUID4_A,
    )

    try:
        lease.acquire()
    except module.RedisNamespaceLeaseLost as exc:
        assert "overflow" in str(exc).lower()
    else:
        raise AssertionError("generation overflow must fail closed")

    assert redis.records == {}


def test_maximum_generation_round_trips_without_json_precision_loss() -> None:
    module = _load_lease_module()
    redis = FakeLeaseRedis()
    redis.fencing_counter = 0xFFFFFFFFFFFE
    lease = module.RedisNamespaceLease(
        redis,
        namespace="trader-TRADER-ACCOUNT-A",
        owner="node-a",
        release_id="release-max",
        persistence_instance_id_factory=lambda: _UUID4_A,
    )

    acquired = lease.acquire()
    loaded = module.get_namespace_lease(redis, acquired.namespace)

    assert acquired.fencing_token == 0xFFFFFFFFFFFF
    assert redis.records[acquired.namespace]["fencing_token"] == (
        "281474976710655"
    )
    assert loaded == acquired


def test_old_owner_cannot_refresh_or_remove_after_takeover() -> None:
    module = _load_lease_module()
    redis = FakeLeaseRedis()
    old = module.RedisNamespaceLease(
        redis,
        namespace="trader-TRADER-ACCOUNT-A",
        owner="node-a",
        release_id="release-old",
        persistence_instance_id_factory=lambda: _UUID4_A,
    )
    old.acquire()
    redis.server_time = 1400
    current = module.RedisNamespaceLease(
        redis,
        namespace="trader-TRADER-ACCOUNT-A",
        owner="node-a",
        release_id="release-new",
        persistence_instance_id_factory=lambda: _UUID4_B,
    )
    current_record = current.acquire()

    try:
        old.refresh()
    except module.RedisNamespaceLeaseLost as exc:
        assert "fenced" in str(exc).lower()
    else:
        raise AssertionError("old lease must be fenced")

    assert old.release() is False
    assert current.refresh() == current_record


def test_fresh_lease_rejects_different_owner_takeover() -> None:
    module = _load_lease_module()
    redis = FakeLeaseRedis()
    module.RedisNamespaceLease(
        redis,
        namespace="trader-TRADER-ACCOUNT-A",
        owner="node-a:process-1",
        release_id="release-abc",
        persistence_instance_id_factory=lambda: _UUID4_A,
    ).acquire()
    redis.server_time = 1010
    contender = module.RedisNamespaceLease(
        redis,
        namespace="trader-TRADER-ACCOUNT-A",
        owner="node-a:process-2",
        release_id="release-abc",
        persistence_instance_id_factory=lambda: _UUID4_B,
    )

    try:
        contender.acquire()
    except module.RedisNamespaceLeaseLost as exc:
        assert "held" in str(exc).lower()
    else:
        raise AssertionError("fresh lease must reject a different owner")


def test_fresh_legacy_registry_member_rejects_takeover() -> None:
    module = _load_lease_module()
    redis = FakeLeaseRedis()
    redis.scores["trader-TRADER-ACCOUNT-A"] = 1000
    contender = module.RedisNamespaceLease(
        redis,
        namespace="trader-TRADER-ACCOUNT-A",
        owner="node-a:process-2",
        release_id="release-abc",
        persistence_instance_id_factory=lambda: _UUID4_A,
    )

    try:
        contender.acquire()
    except module.RedisNamespaceLeaseLost as exc:
        assert "held_legacy" in str(exc).lower()
    else:
        raise AssertionError("fresh legacy registration must block takeover")


def test_remove_without_identity_is_rejected_by_cli() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(REGISTRY_SCRIPT),
            "--redis-url",
            "redis://127.0.0.1:6379/0",
            "remove",
            "trader-TRADER-ACCOUNT-A",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "--owner" in result.stderr
    assert "--release-id" in result.stderr
    assert "--fencing-token" in result.stderr


def test_remove_current_namespace_rejects_identity_free_removal() -> None:
    module = _load_lease_module()
    redis = FakeLeaseRedis()
    namespace = "trader-TRADER-ACCOUNT-A"
    redis.records[namespace] = {
        "namespace": namespace,
        "owner": "node-a",
        "release_id": "release-a",
        "redis_fencing_epoch": _REDIS_FENCING_EPOCH,
        "fencing_token": 7,
        "refreshed_at_epoch": 1000,
    }
    redis.scores[namespace] = 1000

    try:
        module.remove_current_namespace_lease(redis, namespace)
    except module.RedisNamespaceLeaseError as exc:
        assert "identity" in str(exc).lower()
    else:
        raise AssertionError("identity-free lease removal must fail closed")

    assert namespace in redis.records
    assert namespace in redis.scores


def test_fenced_remove_does_not_delete_legacy_registry_member() -> None:
    module = _load_lease_module()
    redis = FakeLeaseRedis()
    namespace = "trader-TRADER-ACCOUNT-A"
    redis.scores[namespace] = 1000
    supplied_identity = module.RedisNamespaceLeaseRecord(
        namespace=namespace,
        owner="unverified-owner",
        release_id="unverified-release",
        fencing_token=7,
        persistence_instance_id=(
            "00000000-0000-4000-8000-000000000007"
        ),
        persistence_namespace=(
            "trader-TRADER-ACCOUNT-A:"
            "00000000-0000-4000-8000-000000000007"
        ),
        refreshed_at_epoch=1000,
        redis_fencing_epoch=_REDIS_FENCING_EPOCH,
    )

    removed = module.remove_namespace_lease(
        redis,
        record=supplied_identity,
    )

    assert removed is False
    assert redis.scores[namespace] == 1000


def test_force_remove_stale_uses_redis_server_time() -> None:
    module = _load_lease_module()
    redis = FakeLeaseRedis()
    namespace = "trader-TRADER-ACCOUNT-A"
    redis.records[namespace] = {
        "namespace": namespace,
        "owner": "node-a",
        "release_id": "release-a",
        "redis_fencing_epoch": _REDIS_FENCING_EPOCH,
        "fencing_token": 7,
        "refreshed_at_epoch": 1000,
    }
    redis.scores[namespace] = 1000

    redis.server_time = 1200
    try:
        module.force_remove_stale_namespace_lease(
            redis,
            namespace,
            max_age_seconds=300,
        )
    except module.RedisNamespaceLeaseError as exc:
        assert "fresh" in str(exc).lower()
    else:
        raise AssertionError("fresh lease must survive force removal")

    redis.server_time = 1401
    removed = module.force_remove_stale_namespace_lease(
        redis,
        namespace,
        max_age_seconds=300,
    )

    assert removed is True
    assert redis.records == {}
    assert redis.scores == {}


def test_force_remove_stale_rejects_corrupt_generation() -> None:
    module = _load_lease_module()
    redis = FakeLeaseRedis()
    namespace = "trader-TRADER-ACCOUNT-A"
    redis.records[namespace] = {
        "namespace": namespace,
        "owner": "node-a",
        "release_id": "release-a",
        "redis_fencing_epoch": _REDIS_FENCING_EPOCH,
        "fencing_token": 7,
        "persistence_instance_id": "invalid",
        "persistence_namespace": f"{namespace}:invalid",
        "refreshed_at_epoch": 1000,
    }
    redis.scores[namespace] = 1000
    redis.server_time = 1401

    with pytest.raises(module.RedisNamespaceLeaseError, match="corrupt"):
        module.force_remove_stale_namespace_lease(
            redis,
            namespace,
            max_age_seconds=300,
        )

    assert namespace in redis.records
    assert namespace in redis.scores


def test_list_retries_when_refresh_changes_snapshot() -> None:
    module = _load_lease_module()

    class RefreshingRedis(FakeLeaseRedis):
        def __init__(self) -> None:
            super().__init__()
            self.score_reads = 0

        def zscore(self, key: str, namespace: str):
            self.score_reads += 1
            if self.score_reads == 1:
                self.records[namespace]["refreshed_at_epoch"] = 1050
                self.scores[namespace] = 1050
            return super().zscore(key, namespace)

    redis = RefreshingRedis()
    namespace = "trader-TRADER-ACCOUNT-A"
    redis.records[namespace] = {
        "namespace": namespace,
        "owner": "node-a",
        "release_id": "release-a",
        "redis_fencing_epoch": _REDIS_FENCING_EPOCH,
        "fencing_token": 7,
        "persistence_instance_id": _UUID4_A,
        "persistence_namespace": f"{namespace}:{_UUID4_A}",
        "refreshed_at_epoch": 1000,
    }
    redis.scores[namespace] = 1000

    records = module.list_active_namespace_leases(
        redis,
        fresh_after_epoch=900,
    )

    assert len(records) == 1
    assert records[0].refreshed_at_epoch == 1050
    assert redis.score_reads == 2


def test_list_retries_when_same_score_generation_changes() -> None:
    module = _load_lease_module()

    class ReplacingRedis(FakeLeaseRedis):
        def __init__(self) -> None:
            super().__init__()
            self.score_reads = 0

        def zscore(self, key: str, namespace: str):
            self.score_reads += 1
            if self.score_reads == 1:
                self.records[namespace] = {
                    "namespace": namespace,
                    "owner": "node-b",
                    "release_id": "release-b",
                    "redis_fencing_epoch": _REDIS_FENCING_EPOCH,
                    "fencing_token": 8,
                    "persistence_instance_id": _UUID4_B,
                    "persistence_namespace": f"{namespace}:{_UUID4_B}",
                    "refreshed_at_epoch": 1000,
                }
            return super().zscore(key, namespace)

    redis = ReplacingRedis()
    namespace = "trader-TRADER-ACCOUNT-A"
    redis.records[namespace] = {
        "namespace": namespace,
        "owner": "node-a",
        "release_id": "release-a",
        "redis_fencing_epoch": _REDIS_FENCING_EPOCH,
        "fencing_token": 7,
        "persistence_instance_id": _UUID4_A,
        "persistence_namespace": f"{namespace}:{_UUID4_A}",
        "refreshed_at_epoch": 1000,
    }
    redis.scores[namespace] = 1000

    records = module.list_active_namespace_leases(
        redis,
        fresh_after_epoch=900,
    )

    assert len(records) == 1
    assert records[0].owner == "node-b"
    assert records[0].persistence_instance_id == _UUID4_B
    assert redis.score_reads == 2


def test_list_fails_closed_after_three_unstable_snapshots() -> None:
    module = _load_lease_module()

    class UnstableRedis(FakeLeaseRedis):
        def __init__(self) -> None:
            super().__init__()
            self.score_reads = 0

        def zscore(self, key: str, namespace: str):
            self.score_reads += 1
            token = 7 + self.score_reads
            instance_id = _UUID4_A
            if token % 2 == 0:
                instance_id = _UUID4_B
            self.records[namespace] = {
                "namespace": namespace,
                "owner": f"node-{token}",
                "release_id": f"release-{token}",
                "redis_fencing_epoch": _REDIS_FENCING_EPOCH,
                "fencing_token": token,
                "persistence_instance_id": instance_id,
                "persistence_namespace": f"{namespace}:{instance_id}",
                "refreshed_at_epoch": 1000,
            }
            return super().zscore(key, namespace)

    redis = UnstableRedis()
    namespace = "trader-TRADER-ACCOUNT-A"
    redis.records[namespace] = {
        "namespace": namespace,
        "owner": "node-a",
        "release_id": "release-a",
        "redis_fencing_epoch": _REDIS_FENCING_EPOCH,
        "fencing_token": 7,
        "persistence_instance_id": _UUID4_A,
        "persistence_namespace": f"{namespace}:{_UUID4_A}",
        "refreshed_at_epoch": 1000,
    }
    redis.scores[namespace] = 1000

    with pytest.raises(
        module.RedisNamespaceLeaseError,
        match="snapshot changed",
    ):
        module.list_active_namespace_leases(
            redis,
            fresh_after_epoch=900,
        )

    assert redis.score_reads == 3


def test_redis_server_time_epoch_uses_redis_time() -> None:
    module = _load_lease_module()
    redis = FakeLeaseRedis()
    redis.server_time = 1700000000

    assert module.redis_server_time_epoch(redis) == 1700000000


def test_cli_list_uses_redis_server_time(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec = importlib.util.spec_from_file_location(
        "redis_namespace_registry",
        REGISTRY_SCRIPT,
    )
    assert spec is not None
    assert spec.loader is not None
    registry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(registry)
    redis = FakeLeaseRedis()
    redis.server_time = 1400
    namespace = "trader-TRADER-ACCOUNT-A"
    redis.records[namespace] = {
        "namespace": namespace,
        "owner": "node-a",
        "release_id": "release-a",
        "redis_fencing_epoch": _REDIS_FENCING_EPOCH,
        "fencing_token": 7,
        "persistence_instance_id": _UUID4_A,
        "persistence_namespace": f"{namespace}:{_UUID4_A}",
        "refreshed_at_epoch": 1200,
    }
    redis.scores[namespace] = 1200
    redis_factory = SimpleNamespace(
        from_url=lambda *_args, **_kwargs: redis,
    )
    monkeypatch.setitem(
        sys.modules,
        "redis",
        SimpleNamespace(Redis=redis_factory),
    )

    result = registry.main(
        [
            "--redis-url",
            "redis://registry.invalid/0",
            "list",
            "--max-age-seconds",
            "300",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert result == 0
    assert output["active_namespaces"] == [namespace]


@pytest.mark.parametrize("command", ("refresh", "remove"))
def test_cli_mutation_uses_stored_epoch_and_random_generation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    spec = importlib.util.spec_from_file_location(
        f"redis_namespace_registry_{command}",
        REGISTRY_SCRIPT,
    )
    assert spec is not None
    assert spec.loader is not None
    registry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(registry)
    redis = FakeLeaseRedis()
    namespace = "trader-TRADER-ACCOUNT-A"
    redis.records[namespace] = {
        "namespace": namespace,
        "owner": "node-a",
        "release_id": "release-a",
        "redis_fencing_epoch": _REDIS_FENCING_EPOCH,
        "fencing_token": 7,
        "persistence_instance_id": _UUID4_A,
        "persistence_namespace": f"{namespace}:{_UUID4_A}",
        "refreshed_at_epoch": 1000,
    }
    redis.scores[namespace] = 1000
    redis_factory = SimpleNamespace(
        from_url=lambda *_args, **_kwargs: redis,
    )
    monkeypatch.setitem(
        sys.modules,
        "redis",
        SimpleNamespace(Redis=redis_factory),
    )

    result = registry.main(
        [
            "--redis-url",
            "redis://registry.invalid/0",
            command,
            namespace,
            "--owner",
            "node-a",
            "--release-id",
            "release-a",
            "--fencing-token",
            "7",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert result == 0
    if command == "refresh":
        assert output["lease"]["redis_fencing_epoch"] == (
            _REDIS_FENCING_EPOCH
        )
        assert output["lease"]["persistence_instance_id"] == _UUID4_A
    else:
        assert output["removed"] is True
        assert namespace not in redis.records


def _persistence_identity(namespace: str, fencing_token: int) -> tuple[str, str]:
    if fencing_token <= 0 or fencing_token > 0xFFFFFFFFFFFF:
        raise AssertionError("invalid fake fencing token")
    instance_id = f"00000000-0000-4000-8000-{fencing_token:012x}"
    return instance_id, f"{namespace}:{instance_id}"


def _record_persistence_identity(
    namespace: str,
    record: dict[str, object],
) -> tuple[str, str] | bool:
    has_instance_id = "persistence_instance_id" in record
    has_persistence_namespace = "persistence_namespace" in record
    if not has_instance_id and not has_persistence_namespace:
        return _persistence_identity(namespace, int(record["fencing_token"]))
    instance_id = record.get("persistence_instance_id")
    persistence_namespace = record.get("persistence_namespace")
    if not has_instance_id:
        if not isinstance(persistence_namespace, str):
            return False
        prefix = f"{namespace}:"
        if not persistence_namespace.startswith(prefix):
            return False
        instance_id = persistence_namespace[len(prefix) :]
    if not _is_canonical_uuid4(instance_id):
        return False
    expected_namespace = f"{namespace}:{instance_id}"
    if not has_persistence_namespace:
        return instance_id, expected_namespace
    if not isinstance(persistence_namespace, str):
        return False
    if persistence_namespace != expected_namespace:
        return False
    return instance_id, expected_namespace


def _record_fencing_token(record: dict[str, object]) -> int | bool:
    value = record.get("fencing_token")
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        token = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        token = int(value)
    else:
        return False
    if token <= 0 or token > 0xFFFFFFFFFFFF:
        return False
    return token


def _record_metadata_valid(
    namespace: str,
    record: dict[str, object],
) -> bool:
    if record.get("namespace") != namespace:
        return False
    for field_name in ("owner", "release_id"):
        value = record.get(field_name)
        if not isinstance(value, str) or not value:
            return False
    refreshed_at = record.get("refreshed_at_epoch")
    if isinstance(refreshed_at, bool) or not isinstance(refreshed_at, int):
        return False
    return refreshed_at > 0


def _owner_matches_node_identity(current_owner: object, owner: str) -> bool:
    if current_owner == owner:
        return True
    if not isinstance(current_owner, str):
        return False
    prefix = owner + ":"
    if not current_owner.startswith(prefix):
        return False
    suffix = current_owner[len(prefix) :]
    parts = suffix.split(":")
    if len(parts) != 3:
        return False
    host, process_id, uuid = parts
    if not host or not process_id.isascii() or not process_id.isdecimal():
        return False
    if len(uuid) != 32:
        return False
    return all(character in "0123456789abcdef" for character in uuid)


def _is_canonical_uuid4(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if value != value.lower() or value != value.strip():
        return False
    try:
        parsed = UUID(value)
    except ValueError:
        return False
    return parsed.version == 4 and str(parsed) == value


def _legacy_instance_id(fencing_token: int) -> str:
    instance_id, _namespace = _persistence_identity(
        "unused",
        fencing_token,
    )
    return instance_id


def _persisted_fencing_token(fencing_token: int) -> int | str:
    if fencing_token > 99999999999999:
        return str(fencing_token)
    return fencing_token


_UUID4_A = "11111111-1111-4111-8111-111111111111"
_UUID4_B = "22222222-2222-4222-8222-222222222222"
