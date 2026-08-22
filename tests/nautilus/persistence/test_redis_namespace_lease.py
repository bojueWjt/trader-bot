from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import UUID

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
sys.path.insert(0, str(SERVICE_ROOT))

import persistence.redis_namespace_lease as lease_module  # noqa: E402
from persistence.redis_namespace_lease import (  # noqa: E402
    RedisNamespaceLease,
    RedisNamespaceLeaseError,
    RedisNamespaceLeaseLost,
    derive_persistence_instance_id,
    derive_persistence_namespace,
)


ACCOUNT_A_NAMESPACE = "trader-TRADER-ACCOUNT-A"
FIRST_INSTANCE_ID = "11111111-1111-4111-8111-111111111111"
SECOND_INSTANCE_ID = "22222222-2222-4222-8222-222222222222"
FIRST_REDIS_FENCING_EPOCH = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SECOND_REDIS_FENCING_EPOCH = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class _ServerTimeLeaseRedis:
    def __init__(self, *, server_time: int = 1000) -> None:
        self.server_time = server_time
        self.redis_fencing_epoch: str | None = FIRST_REDIS_FENCING_EPOCH
        self.fencing_counter = 0
        self.records: dict[str, dict[str, object]] = {}
        self.scores: dict[str, int] = {}
        self.eval_calls: list[tuple[object, ...]] = []
        self.fail_after_next_acquire = False

    def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: object,
    ) -> object:
        self.eval_calls.append(keys_and_args)
        if "redis-namespace-lease:acquire-v3" in script:
            result = self._acquire(numkeys, keys_and_args)
            if self.fail_after_next_acquire:
                self.fail_after_next_acquire = False
                raise TimeoutError("response lost after acquire commit")
            return result
        if "redis-namespace-lease:refresh-v3" in script:
            return self._refresh(numkeys, keys_and_args)
        if "redis-namespace-lease:remove-v3" in script:
            return self._remove(numkeys, keys_and_args)
        if "redis-namespace-lease:force-remove-stale-v1" in script:
            return self._force_remove(numkeys, keys_and_args)
        if "redis-namespace-lease:acquire-v7" in script:
            result = self._acquire(numkeys, keys_and_args)
            if self.fail_after_next_acquire:
                self.fail_after_next_acquire = False
                raise TimeoutError("response lost after acquire commit")
            return result
        if "redis-namespace-lease:acquire-v4" in script:
            result = self._acquire(numkeys, keys_and_args)
            if self.fail_after_next_acquire:
                self.fail_after_next_acquire = False
                raise TimeoutError("response lost after acquire commit")
            return result
        if "redis-namespace-lease:refresh-v5" in script:
            return self._refresh(numkeys, keys_and_args)
        if "redis-namespace-lease:remove-v4" in script:
            return self._remove(numkeys, keys_and_args)
        if "redis-namespace-lease:force-remove-stale-v3" in script:
            return self._force_remove(numkeys, keys_and_args)
        raise AssertionError("unexpected Redis lease script")

    def hget(self, key: str, field: str) -> str | None:
        del key
        record = self.records.get(field)
        if record is None:
            return None
        return json.dumps(record, sort_keys=True)

    def zrangebyscore(
        self,
        key: str,
        minimum: int,
        maximum: str,
        *,
        withscores: bool,
    ) -> list[tuple[str, float]]:
        del key, maximum
        if not withscores:
            raise AssertionError("lease listing must request scores")
        return [
            (namespace, float(score))
            for namespace, score in sorted(self.scores.items())
            if score >= minimum
        ]

    def lose_registry_state(self) -> None:
        self.fencing_counter = 0
        self.records.clear()
        self.scores.clear()

    def replace_redis_epoch(self, value: str | None) -> None:
        self.redis_fencing_epoch = value
        self.lose_registry_state()

    def restart(self) -> None:
        return

    def _acquire(
        self,
        numkeys: int,
        values: tuple[object, ...],
    ) -> list[object]:
        if numkeys == 4:
            (
                _registry_key,
                _metadata_key,
                _fencing_key,
                epoch_key,
                raw_namespace,
                raw_owner,
                raw_release_id,
                raw_max_age_seconds,
                raw_candidate,
            ) = values
            assert epoch_key == lease_module.DEFAULT_REDIS_FENCING_EPOCH_KEY
            epoch_error = self._epoch_error()
            if epoch_error:
                return [0, 0, epoch_error]
        else:
            assert numkeys == 3
            (
                _registry_key,
                _metadata_key,
                _fencing_key,
                raw_namespace,
                raw_owner,
                raw_release_id,
                raw_max_age_seconds,
                raw_candidate,
            ) = values
        namespace = str(raw_namespace)
        owner = str(raw_owner)
        release_id = str(raw_release_id)
        max_age_seconds = int(raw_max_age_seconds)
        candidate = str(raw_candidate)
        current = self.records.get(namespace)
        current_score = self.scores.get(namespace)
        fresh_after_epoch = self.server_time - max_age_seconds
        if current is not None:
            current_token = int(current["fencing_token"])
            if numkeys == 4:
                current_epoch = current.get("redis_fencing_epoch")
                if not self._is_canonical_uuid4(current_epoch):
                    return [0, current_token, "CORRUPT_EPOCH"]
                if current_epoch != self.redis_fencing_epoch:
                    return [0, current_token, "EPOCH_MISMATCH"]
            same_identity = (
                current["owner"] == owner
                and current["release_id"] == release_id
                and current["persistence_instance_id"] == candidate
            )
            current_is_fresh = (
                current_score is not None
                and current_score > fresh_after_epoch
            )
            if same_identity and current_is_fresh:
                current["refreshed_at_epoch"] = self.server_time
                self.scores[namespace] = self.server_time
                return self._acquired_result(current)
            if current_is_fresh:
                return [0, current_token, "HELD"]
            if not _owner_matches_node_identity(current["owner"], owner):
                return [0, current_token, "STALE_FOREIGN"]
        elif current_score is not None and current_score > fresh_after_epoch:
            return [0, 0, "HELD_LEGACY"]

        self.fencing_counter += 1
        persistence_namespace = f"{namespace}:{candidate}"
        record = {
            "namespace": namespace,
            "owner": owner,
            "release_id": release_id,
            "fencing_token": self.fencing_counter,
            "persistence_instance_id": candidate,
            "persistence_namespace": persistence_namespace,
            "refreshed_at_epoch": self.server_time,
        }
        if numkeys == 4:
            record["redis_fencing_epoch"] = self.redis_fencing_epoch
        self.records[namespace] = record
        self.scores[namespace] = self.server_time
        return self._acquired_result(record)

    def _refresh(
        self,
        numkeys: int,
        values: tuple[object, ...],
    ) -> list[object]:
        raw_record_epoch: object | None = None
        if numkeys == 3:
            (
                _registry_key,
                _metadata_key,
                epoch_key,
                raw_namespace,
                raw_owner,
                raw_release_id,
                raw_token,
                raw_max_age_seconds,
                raw_record_epoch,
            ) = values
            assert epoch_key == lease_module.DEFAULT_REDIS_FENCING_EPOCH_KEY
            epoch_error = self._epoch_error()
            if epoch_error:
                return [0, epoch_error, self.server_time]
            if str(raw_record_epoch) != self.redis_fencing_epoch:
                return [0, "EPOCH_CHANGED", self.server_time]
        else:
            assert numkeys == 2
            (
                _registry_key,
                _metadata_key,
                raw_namespace,
                raw_owner,
                raw_release_id,
                raw_token,
                raw_max_age_seconds,
            ) = values
        namespace = str(raw_namespace)
        current = self.records.get(namespace)
        if current is None:
            return [0, "MISSING", self.server_time]
        if numkeys == 3:
            current_epoch = current.get("redis_fencing_epoch")
            if not self._is_canonical_uuid4(current_epoch):
                return [0, "CORRUPT_EPOCH", self.server_time]
            if current_epoch != raw_record_epoch:
                return [0, "EPOCH_MISMATCH", self.server_time]
        score = self.scores.get(namespace)
        if score is None:
            return [0, "CORRUPT", self.server_time]
        max_age_seconds = int(raw_max_age_seconds)
        if score <= self.server_time - max_age_seconds:
            return [0, "EXPIRED", self.server_time]
        expected_identity = (
            str(raw_owner),
            str(raw_release_id),
            int(raw_token),
        )
        actual_identity = (
            current["owner"],
            current["release_id"],
            int(current["fencing_token"]),
        )
        if actual_identity != expected_identity:
            return [0, "FENCED", self.server_time]
        current["refreshed_at_epoch"] = self.server_time
        self.scores[namespace] = self.server_time
        result = [
            1,
            "REFRESHED",
            self.server_time,
            current["persistence_instance_id"],
            current["persistence_namespace"],
        ]
        if numkeys == 3:
            result.append(current["redis_fencing_epoch"])
        return result

    def _remove(
        self,
        numkeys: int,
        values: tuple[object, ...],
    ) -> list[object]:
        raw_record_epoch: object | None = None
        if numkeys == 3:
            (
                _registry_key,
                _metadata_key,
                epoch_key,
                raw_namespace,
                raw_owner,
                raw_release_id,
                raw_token,
                raw_record_epoch,
            ) = values
            assert epoch_key == lease_module.DEFAULT_REDIS_FENCING_EPOCH_KEY
            epoch_error = self._epoch_error()
            if epoch_error:
                return [0, epoch_error]
            if str(raw_record_epoch) != self.redis_fencing_epoch:
                return [0, "EPOCH_CHANGED"]
        else:
            assert numkeys == 2
            (
                _registry_key,
                _metadata_key,
                raw_namespace,
                raw_owner,
                raw_release_id,
                raw_token,
            ) = values
        namespace = str(raw_namespace)
        current = self.records.get(namespace)
        if current is None:
            return [0, "MISSING_IDENTITY"]
        if numkeys == 3:
            current_epoch = current.get("redis_fencing_epoch")
            if not self._is_canonical_uuid4(current_epoch):
                return [0, "CORRUPT_EPOCH"]
            if current_epoch != raw_record_epoch:
                return [0, "EPOCH_MISMATCH"]
        expected_identity = (
            str(raw_owner),
            str(raw_release_id),
            int(raw_token),
        )
        actual_identity = (
            current["owner"],
            current["release_id"],
            int(current["fencing_token"]),
        )
        if actual_identity != expected_identity:
            return [0, "FENCED"]
        del self.records[namespace]
        self.scores.pop(namespace, None)
        return [1, "REMOVED"]

    def _force_remove(
        self,
        numkeys: int,
        values: tuple[object, ...],
    ) -> list[object]:
        if numkeys == 3:
            (
                _registry_key,
                _metadata_key,
                epoch_key,
                raw_namespace,
                raw_max_age_seconds,
            ) = values
            assert epoch_key == lease_module.DEFAULT_REDIS_FENCING_EPOCH_KEY
            epoch_error = self._epoch_error()
            if epoch_error:
                return [0, epoch_error, self.server_time]
        else:
            assert numkeys == 2
            (
                _registry_key,
                _metadata_key,
                raw_namespace,
                raw_max_age_seconds,
            ) = values
        namespace = str(raw_namespace)
        current = self.records.get(namespace)
        if current is None:
            return [0, "MISSING", self.server_time]
        if numkeys == 3:
            current_epoch = current.get("redis_fencing_epoch")
            if not self._is_canonical_uuid4(current_epoch):
                return [0, "CORRUPT_EPOCH", self.server_time]
            if current_epoch != self.redis_fencing_epoch:
                return [0, "EPOCH_MISMATCH", self.server_time]
        score = self.scores.get(namespace)
        if score is None:
            return [0, "CORRUPT", self.server_time]
        max_age_seconds = int(raw_max_age_seconds)
        if score > self.server_time - max_age_seconds:
            return [0, "FRESH", self.server_time]
        del self.records[namespace]
        del self.scores[namespace]
        return [1, "REMOVED_STALE", self.server_time]

    def _epoch_error(self) -> str:
        if self.redis_fencing_epoch is None:
            return "MISSING_EPOCH"
        if not self._is_canonical_uuid4(self.redis_fencing_epoch):
            return "CORRUPT_EPOCH"
        return ""

    @staticmethod
    def _is_canonical_uuid4(value: object) -> bool:
        if not isinstance(value, str):
            return False
        try:
            parsed = UUID(value)
        except ValueError:
            return False
        return parsed.version == 4 and str(parsed) == value

    @staticmethod
    def _acquired_result(record: dict[str, object]) -> list[object]:
        result = [
            1,
            record["fencing_token"],
            record["refreshed_at_epoch"],
            record["persistence_instance_id"],
            record["persistence_namespace"],
        ]
        if "redis_fencing_epoch" in record:
            result.append(record["redis_fencing_epoch"])
        return result


class _CandidateFactory:
    def __init__(self, *values: str) -> None:
        self._values = list(values)
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if not self._values:
            raise AssertionError("candidate factory exhausted")
        return self._values.pop(0)


class _StaticEvalRedis:
    def __init__(self, result: object) -> None:
        self.result = result
        self.eval_calls: list[tuple[object, ...]] = []

    def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: object,
    ) -> object:
        del script, numkeys
        self.eval_calls.append(keys_and_args)
        return self.result


def test_fencing_token_maps_to_uuid4_persistence_generation() -> None:
    instance_id = derive_persistence_instance_id(1)
    persistence_namespace = derive_persistence_namespace(
        ACCOUNT_A_NAMESPACE,
        1,
    )

    assert instance_id == "00000000-0000-4000-8000-000000000001"
    assert UUID(instance_id).version == 4
    assert persistence_namespace == (
        "trader-TRADER-ACCOUNT-A:"
        "00000000-0000-4000-8000-000000000001"
    )


def test_maximum_fencing_token_maps_without_truncation() -> None:
    assert derive_persistence_instance_id(0xFFFFFFFFFFFF) == (
        "00000000-0000-4000-8000-ffffffffffff"
    )


@pytest.mark.parametrize(
    "fencing_token",
    (0, -1, 0x1000000000000),
)
def test_invalid_fencing_token_fails_closed(fencing_token: int) -> None:
    with pytest.raises(
        RedisNamespaceLeaseError,
        match="fencing_token",
    ):
        derive_persistence_instance_id(fencing_token)


def test_redis_fencing_epoch_marker_key_is_fixed() -> None:
    assert lease_module.DEFAULT_REDIS_FENCING_EPOCH_KEY == (
        "trader-bot:redis-fencing-epoch"
    )


@pytest.mark.parametrize(
    "redis_fencing_epoch",
    (
        None,
        "",
        "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
        "aaaaaaaa-aaaa-1aaa-8aaa-aaaaaaaaaaaa",
        "aaaaaaaa-aaaa-4aaa-7aaa-aaaaaaaaaaaa",
        "not-a-uuid",
    ),
)
def test_acquire_fails_closed_when_epoch_marker_is_missing_or_corrupt(
    redis_fencing_epoch: str | None,
) -> None:
    redis = _ServerTimeLeaseRedis()
    redis.redis_fencing_epoch = redis_fencing_epoch
    lease = RedisNamespaceLease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-a:process-1",
        release_id="release-a",
        persistence_instance_id_factory=_CandidateFactory(FIRST_INSTANCE_ID),
    )

    with pytest.raises(
        RedisNamespaceLeaseLost,
        match="epoch",
    ):
        lease.acquire()

    assert redis.records == {}
    assert redis.fencing_counter == 0


@pytest.mark.parametrize(
    "metadata_epoch",
    (
        None,
        SECOND_REDIS_FENCING_EPOCH,
    ),
)
def test_acquire_fails_closed_when_metadata_epoch_is_missing_or_mismatched(
    metadata_epoch: str | None,
) -> None:
    redis = _ServerTimeLeaseRedis()
    redis.records[ACCOUNT_A_NAMESPACE] = {
        "namespace": ACCOUNT_A_NAMESPACE,
        "owner": "node-a:process-1",
        "release_id": "release-a",
        "fencing_token": 7,
        "persistence_instance_id": FIRST_INSTANCE_ID,
        "persistence_namespace": (
            f"{ACCOUNT_A_NAMESPACE}:{FIRST_INSTANCE_ID}"
        ),
        "refreshed_at_epoch": redis.server_time,
    }
    if metadata_epoch is not None:
        redis.records[ACCOUNT_A_NAMESPACE]["redis_fencing_epoch"] = (
            metadata_epoch
        )
    redis.scores[ACCOUNT_A_NAMESPACE] = redis.server_time
    lease = RedisNamespaceLease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-a:process-1",
        release_id="release-a",
        persistence_instance_id_factory=_CandidateFactory(SECOND_INSTANCE_ID),
    )

    with pytest.raises(
        RedisNamespaceLeaseLost,
        match="epoch",
    ):
        lease.acquire()

    assert redis.records[ACCOUNT_A_NAMESPACE]["fencing_token"] == 7


def test_acquire_uses_redis_server_time_for_freshness() -> None:
    redis = _ServerTimeLeaseRedis(server_time=1000)
    first = RedisNamespaceLease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-a",
        release_id="release-a",
        max_age_seconds=300,
        clock_fn=lambda: 1,
        persistence_instance_id_factory=_CandidateFactory(FIRST_INSTANCE_ID),
    )
    contender = RedisNamespaceLease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-a",
        release_id="release-b",
        max_age_seconds=300,
        clock_fn=lambda: 9999999999,
        persistence_instance_id_factory=_CandidateFactory(SECOND_INSTANCE_ID),
    )

    acquired = first.acquire()

    with pytest.raises(RedisNamespaceLeaseLost, match="held"):
        contender.acquire()

    assert acquired.redis_fencing_epoch == FIRST_REDIS_FENCING_EPOCH
    assert acquired.refreshed_at_epoch == 1000
    assert redis.scores[ACCOUNT_A_NAMESPACE] == 1000

    redis.server_time = 1301
    replacement = contender.acquire()

    assert replacement.fencing_token == 2
    assert replacement.refreshed_at_epoch == 1301
    assert replacement.persistence_instance_id == SECOND_INSTANCE_ID


def test_replacement_start_before_old_stop_fences_resumed_old_runtime() -> None:
    redis = _ServerTimeLeaseRedis(server_time=1000)
    old_runtime = RedisNamespaceLease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-a",
        release_id="release-a",
        max_age_seconds=300,
        persistence_instance_id_factory=_CandidateFactory(FIRST_INSTANCE_ID),
    )
    replacement = RedisNamespaceLease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-a",
        release_id="release-b",
        max_age_seconds=300,
        persistence_instance_id_factory=_CandidateFactory(SECOND_INSTANCE_ID),
    )
    old_runtime.acquire()

    with pytest.raises(RedisNamespaceLeaseLost, match="held"):
        replacement.acquire()

    redis.server_time = 1301
    replacement_record = replacement.acquire()

    with pytest.raises(RedisNamespaceLeaseLost, match="fenced"):
        old_runtime.refresh()
    assert old_runtime.release() is False

    current = lease_module.get_namespace_lease(
        redis,
        ACCOUNT_A_NAMESPACE,
    )
    assert current == replacement_record
    assert replacement_record.fencing_token == 2


def test_fresh_same_owner_new_process_candidate_cannot_reuse_token() -> None:
    redis = _ServerTimeLeaseRedis(server_time=1000)
    first = RedisNamespaceLease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-a",
        release_id="release-a",
        max_age_seconds=300,
        persistence_instance_id_factory=_CandidateFactory(FIRST_INSTANCE_ID),
    )
    replacement = RedisNamespaceLease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-a",
        release_id="release-a",
        max_age_seconds=300,
        persistence_instance_id_factory=_CandidateFactory(SECOND_INSTANCE_ID),
    )
    first_record = first.acquire()

    with pytest.raises(RedisNamespaceLeaseLost, match="held"):
        replacement.acquire()

    current = lease_module.get_namespace_lease(
        redis,
        ACCOUNT_A_NAMESPACE,
    )
    assert current == first_record
    assert current.refreshed_at_epoch == 1000

    redis.server_time = 1301
    replacement_record = replacement.acquire()

    assert replacement_record.fencing_token == 2
    assert replacement_record.persistence_instance_id == SECOND_INSTANCE_ID


def test_same_node_restart_takes_over_at_two_minute_boundary() -> None:
    redis = _ServerTimeLeaseRedis(server_time=1000)
    RedisNamespaceLease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-a",
        release_id="release-a",
        max_age_seconds=120,
        persistence_instance_id_factory=_CandidateFactory(FIRST_INSTANCE_ID),
    ).acquire()
    replacement = RedisNamespaceLease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-a",
        release_id="release-a",
        max_age_seconds=120,
        persistence_instance_id_factory=_CandidateFactory(SECOND_INSTANCE_ID),
    )

    redis.server_time = 1119
    with pytest.raises(RedisNamespaceLeaseLost, match="held"):
        replacement.acquire()

    redis.server_time = 1120
    replacement_record = replacement.acquire()

    assert replacement_record.fencing_token == 2
    assert replacement_record.persistence_instance_id == SECOND_INSTANCE_ID


def test_stale_foreign_owner_cannot_acquire_namespace() -> None:
    redis = _ServerTimeLeaseRedis(server_time=1000)
    RedisNamespaceLease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-a",
        release_id="release-a",
        max_age_seconds=300,
        persistence_instance_id_factory=_CandidateFactory(FIRST_INSTANCE_ID),
    ).acquire()
    foreign = RedisNamespaceLease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-b",
        release_id="release-b",
        max_age_seconds=300,
        persistence_instance_id_factory=_CandidateFactory(SECOND_INSTANCE_ID),
    )

    redis.server_time = 1301

    with pytest.raises(RedisNamespaceLeaseLost, match="foreign"):
        foreign.acquire()

    current = lease_module.get_namespace_lease(
        redis,
        ACCOUNT_A_NAMESPACE,
    )
    assert current is not False
    assert current.owner == "node-a"
    assert current.fencing_token == 1


def test_stale_legacy_owner_from_same_node_can_be_taken_over() -> None:
    redis = _ServerTimeLeaseRedis(server_time=1000)
    RedisNamespaceLease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-a:jp-24:1234:" + ("a" * 32),
        release_id="release-old",
        max_age_seconds=300,
        persistence_instance_id_factory=_CandidateFactory(FIRST_INSTANCE_ID),
    ).acquire()
    replacement = RedisNamespaceLease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-a",
        release_id="release-new",
        max_age_seconds=300,
        persistence_instance_id_factory=_CandidateFactory(SECOND_INSTANCE_ID),
    )

    redis.server_time = 1301

    record = replacement.acquire()

    assert record.owner == "node-a"
    assert record.release_id == "release-new"
    assert record.fencing_token == 2
    assert record.persistence_instance_id == SECOND_INSTANCE_ID


def test_refresh_uses_redis_time_and_cannot_resurrect_expired_lease() -> None:
    redis = _ServerTimeLeaseRedis(server_time=1000)
    lease = RedisNamespaceLease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-a:process-1",
        release_id="release-a",
        max_age_seconds=300,
        clock_fn=lambda: 9999999999,
        persistence_instance_id_factory=_CandidateFactory(FIRST_INSTANCE_ID),
    )
    lease.acquire()

    redis.server_time = 1050
    refreshed = lease.refresh()

    assert refreshed.refreshed_at_epoch == 1050

    redis.server_time = 1351
    with pytest.raises(RedisNamespaceLeaseLost, match="expired"):
        lease.refresh()

    assert redis.scores[ACCOUNT_A_NAMESPACE] == 1050


def test_refresh_continues_across_restart_when_epoch_marker_is_preserved() -> None:
    redis = _ServerTimeLeaseRedis(server_time=1000)
    lease = RedisNamespaceLease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-a:process-1",
        release_id="release-a",
        persistence_instance_id_factory=_CandidateFactory(FIRST_INSTANCE_ID),
    )
    acquired = lease.acquire()

    redis.restart()
    redis.server_time = 1050
    refreshed = lease.refresh()

    assert refreshed.redis_fencing_epoch == acquired.redis_fencing_epoch
    assert refreshed.refreshed_at_epoch == 1050


def test_refresh_fails_closed_after_redis_epoch_changes() -> None:
    redis = _ServerTimeLeaseRedis(server_time=1000)
    lease = RedisNamespaceLease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-a:process-1",
        release_id="release-a",
        persistence_instance_id_factory=_CandidateFactory(FIRST_INSTANCE_ID),
    )
    acquired = lease.acquire()
    redis.redis_fencing_epoch = SECOND_REDIS_FENCING_EPOCH

    with pytest.raises(
        RedisNamespaceLeaseLost,
        match="epoch",
    ):
        lease.refresh()

    assert lease.record is acquired
    assert redis.records[ACCOUNT_A_NAMESPACE]["redis_fencing_epoch"] == (
        FIRST_REDIS_FENCING_EPOCH
    )


def test_remove_fails_closed_after_redis_epoch_changes() -> None:
    redis = _ServerTimeLeaseRedis(server_time=1000)
    lease = RedisNamespaceLease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-a:process-1",
        release_id="release-a",
        persistence_instance_id_factory=_CandidateFactory(FIRST_INSTANCE_ID),
    )
    acquired = lease.acquire()
    redis.redis_fencing_epoch = SECOND_REDIS_FENCING_EPOCH

    assert lease.release() is False
    assert lease.record is acquired
    assert ACCOUNT_A_NAMESPACE in redis.records


def test_force_remove_fails_closed_when_metadata_epoch_mismatches_marker() -> None:
    redis = _ServerTimeLeaseRedis(server_time=1000)
    RedisNamespaceLease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-a:process-1",
        release_id="release-a",
        max_age_seconds=300,
        persistence_instance_id_factory=_CandidateFactory(FIRST_INSTANCE_ID),
    ).acquire()
    redis.server_time = 1301
    redis.redis_fencing_epoch = SECOND_REDIS_FENCING_EPOCH

    with pytest.raises(
        RedisNamespaceLeaseError,
        match="epoch",
    ):
        lease_module.force_remove_stale_namespace_lease(
            redis,
            ACCOUNT_A_NAMESPACE,
            max_age_seconds=300,
        )

    assert ACCOUNT_A_NAMESPACE in redis.records


def test_acquire_retry_reuses_process_generated_uuid4_candidate() -> None:
    redis = _ServerTimeLeaseRedis(server_time=1000)
    redis.fail_after_next_acquire = True
    factory = _CandidateFactory(FIRST_INSTANCE_ID)
    lease = RedisNamespaceLease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-a:process-1",
        release_id="release-a",
        persistence_instance_id_factory=factory,
    )

    with pytest.raises(TimeoutError, match="response lost"):
        lease.acquire()

    acquired = lease.acquire()

    assert factory.calls == 1
    assert acquired.fencing_token == 1
    assert acquired.persistence_instance_id == FIRST_INSTANCE_ID
    assert len(redis.eval_calls) == 2


def test_expired_reacquisition_retry_reuses_candidate_after_response_loss() -> None:
    redis = _ServerTimeLeaseRedis(server_time=1000)
    factory = _CandidateFactory(FIRST_INSTANCE_ID, SECOND_INSTANCE_ID)
    lease = RedisNamespaceLease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-a:process-1",
        release_id="release-a",
        max_age_seconds=300,
        persistence_instance_id_factory=factory,
    )
    lease.acquire()

    redis.server_time = 1301
    redis.fail_after_next_acquire = True
    with pytest.raises(TimeoutError, match="response lost"):
        lease.acquire()

    reacquired = lease.acquire()

    assert factory.calls == 2
    assert reacquired.fencing_token == 2
    assert reacquired.persistence_instance_id == SECOND_INSTANCE_ID
    assert redis.records[ACCOUNT_A_NAMESPACE]["persistence_instance_id"] == (
        SECOND_INSTANCE_ID
    )


def test_different_redis_epoch_can_reuse_token_with_new_namespace() -> None:
    redis = _ServerTimeLeaseRedis(server_time=1000)
    first = RedisNamespaceLease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-a:process-1",
        release_id="release-a",
        persistence_instance_id_factory=_CandidateFactory(FIRST_INSTANCE_ID),
    ).acquire()

    redis.replace_redis_epoch(SECOND_REDIS_FENCING_EPOCH)
    redis.server_time = 1001
    replacement = RedisNamespaceLease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-a:process-2",
        release_id="release-b",
        persistence_instance_id_factory=_CandidateFactory(SECOND_INSTANCE_ID),
    ).acquire()

    assert first.fencing_token == replacement.fencing_token == 1
    assert first.redis_fencing_epoch == FIRST_REDIS_FENCING_EPOCH
    assert replacement.redis_fencing_epoch == SECOND_REDIS_FENCING_EPOCH
    assert first.persistence_instance_id == FIRST_INSTANCE_ID
    assert replacement.persistence_instance_id == SECOND_INSTANCE_ID
    assert first.persistence_namespace != replacement.persistence_namespace


def test_redis_flush_rebaseline_fences_old_epoch_after_counter_restart() -> None:
    redis = _ServerTimeLeaseRedis(server_time=1000)
    old_runtime = RedisNamespaceLease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-a:process-old",
        release_id="release-a",
        persistence_instance_id_factory=_CandidateFactory(FIRST_INSTANCE_ID),
    )
    old_record = old_runtime.acquire()

    redis.replace_redis_epoch(SECOND_REDIS_FENCING_EPOCH)
    replacement = RedisNamespaceLease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-a:process-new",
        release_id="release-b",
        persistence_instance_id_factory=_CandidateFactory(SECOND_INSTANCE_ID),
    )
    replacement_record = replacement.acquire()

    with pytest.raises(RedisNamespaceLeaseLost, match="epoch"):
        old_runtime.refresh()
    assert old_runtime.release() is False

    current = lease_module.get_namespace_lease(
        redis,
        ACCOUNT_A_NAMESPACE,
    )
    assert current == replacement_record
    assert old_record.fencing_token == replacement_record.fencing_token == 1
    assert old_record.redis_fencing_epoch == FIRST_REDIS_FENCING_EPOCH
    assert (
        replacement_record.redis_fencing_epoch
        == SECOND_REDIS_FENCING_EPOCH
    )


def test_expired_same_process_acquisition_uses_a_new_candidate() -> None:
    redis = _ServerTimeLeaseRedis(server_time=1000)
    factory = _CandidateFactory(FIRST_INSTANCE_ID, SECOND_INSTANCE_ID)
    lease = RedisNamespaceLease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-a:process-1",
        release_id="release-a",
        max_age_seconds=300,
        persistence_instance_id_factory=factory,
    )
    first = lease.acquire()

    redis.server_time = 1301
    replacement = lease.acquire()

    assert factory.calls == 2
    assert first.fencing_token == 1
    assert replacement.fencing_token == 2
    assert first.persistence_instance_id == FIRST_INSTANCE_ID
    assert replacement.persistence_instance_id == SECOND_INSTANCE_ID


def test_legacy_epoch_arguments_remain_accepted_but_never_reach_lua() -> None:
    redis = _ServerTimeLeaseRedis(server_time=1000)

    acquired = lease_module.acquire_namespace_lease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-a:process-1",
        release_id="release-a",
        refreshed_at_epoch=9999999999,
        max_age_seconds=300,
        persistence_instance_id_candidate=FIRST_INSTANCE_ID,
    )

    assert acquired.refreshed_at_epoch == 1000
    assert redis.eval_calls[-1][-2:] == (300, FIRST_INSTANCE_ID)

    redis.server_time = 1050
    refreshed = lease_module.refresh_namespace_lease(
        redis,
        record=acquired,
        refreshed_at_epoch=8888888888,
        max_age_seconds=300,
    )

    assert refreshed.refreshed_at_epoch == 1050
    assert redis.eval_calls[-1][-3:] == (
        1,
        300,
        FIRST_REDIS_FENCING_EPOCH,
    )


def test_random_uuid4_generation_metadata_round_trips() -> None:
    redis = _ServerTimeLeaseRedis(server_time=1000)
    persistence_namespace = f"{ACCOUNT_A_NAMESPACE}:{FIRST_INSTANCE_ID}"
    redis.records[ACCOUNT_A_NAMESPACE] = {
        "namespace": ACCOUNT_A_NAMESPACE,
        "owner": "node-a:process-1",
        "release_id": "release-a",
        "redis_fencing_epoch": FIRST_REDIS_FENCING_EPOCH,
        "fencing_token": 7,
        "persistence_instance_id": FIRST_INSTANCE_ID,
        "persistence_namespace": persistence_namespace,
        "refreshed_at_epoch": 1000,
    }
    redis.scores[ACCOUNT_A_NAMESPACE] = 1000

    record = lease_module.get_namespace_lease(
        redis,
        ACCOUNT_A_NAMESPACE,
    )

    assert record is not False
    assert record.fencing_token == 7
    assert record.redis_fencing_epoch == FIRST_REDIS_FENCING_EPOCH
    assert record.persistence_instance_id == FIRST_INSTANCE_ID
    assert record.persistence_namespace == persistence_namespace


def test_maximum_fencing_token_string_metadata_round_trips_exactly() -> None:
    redis = _ServerTimeLeaseRedis(server_time=1000)
    persistence_namespace = f"{ACCOUNT_A_NAMESPACE}:{FIRST_INSTANCE_ID}"
    redis.records[ACCOUNT_A_NAMESPACE] = {
        "namespace": ACCOUNT_A_NAMESPACE,
        "owner": "node-a:process-1",
        "release_id": "release-a",
        "redis_fencing_epoch": FIRST_REDIS_FENCING_EPOCH,
        "fencing_token": str(
            lease_module.MAX_PERSISTENCE_GENERATION_FENCING_TOKEN
        ),
        "persistence_instance_id": FIRST_INSTANCE_ID,
        "persistence_namespace": persistence_namespace,
        "refreshed_at_epoch": 1000,
    }

    record = lease_module.get_namespace_lease(
        redis,
        ACCOUNT_A_NAMESPACE,
    )

    assert record is not False
    assert record.fencing_token == 0xFFFFFFFFFFFF
    assert record.redis_fencing_epoch == FIRST_REDIS_FENCING_EPOCH
    assert record.persistence_instance_id == FIRST_INSTANCE_ID
    assert record.persistence_namespace == persistence_namespace


def test_registry_placeholder_refresh_uses_stored_random_generation() -> None:
    redis = _ServerTimeLeaseRedis(server_time=1000)
    acquired = RedisNamespaceLease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-a:process-1",
        release_id="release-a",
        persistence_instance_id_factory=_CandidateFactory(FIRST_INSTANCE_ID),
    ).acquire()
    placeholder = lease_module.RedisNamespaceLeaseRecord(
        namespace=acquired.namespace,
        owner=acquired.owner,
        release_id=acquired.release_id,
        fencing_token=acquired.fencing_token,
        persistence_instance_id=derive_persistence_instance_id(
            acquired.fencing_token
        ),
        persistence_namespace=derive_persistence_namespace(
            acquired.namespace,
            acquired.fencing_token,
        ),
        refreshed_at_epoch=acquired.refreshed_at_epoch,
        redis_fencing_epoch=acquired.redis_fencing_epoch,
    )

    redis.server_time = 1050
    refreshed = lease_module.refresh_namespace_lease(
        redis,
        record=placeholder,
    )

    assert refreshed.persistence_instance_id == FIRST_INSTANCE_ID
    assert refreshed.persistence_namespace == acquired.persistence_namespace


def test_registry_placeholder_remove_uses_stored_fencing_identity() -> None:
    redis = _ServerTimeLeaseRedis(server_time=1000)
    acquired = RedisNamespaceLease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-a:process-1",
        release_id="release-a",
        persistence_instance_id_factory=_CandidateFactory(FIRST_INSTANCE_ID),
    ).acquire()
    placeholder = lease_module.RedisNamespaceLeaseRecord(
        namespace=acquired.namespace,
        owner=acquired.owner,
        release_id=acquired.release_id,
        fencing_token=acquired.fencing_token,
        persistence_instance_id=derive_persistence_instance_id(
            acquired.fencing_token
        ),
        persistence_namespace=derive_persistence_namespace(
            acquired.namespace,
            acquired.fencing_token,
        ),
        refreshed_at_epoch=acquired.refreshed_at_epoch,
        redis_fencing_epoch=acquired.redis_fencing_epoch,
    )

    removed = lease_module.remove_namespace_lease(
        redis,
        record=placeholder,
    )

    assert removed is True
    assert ACCOUNT_A_NAMESPACE not in redis.records
    assert ACCOUNT_A_NAMESPACE not in redis.scores


@pytest.mark.parametrize(
    ("persistence_instance_id", "persistence_namespace"),
    (
        (None, None),
        (
            "11111111-1111-1111-8111-111111111111",
            (
                "trader-TRADER-ACCOUNT-A:"
                "11111111-1111-1111-8111-111111111111"
            ),
        ),
        (
            FIRST_INSTANCE_ID,
            f"{ACCOUNT_A_NAMESPACE}:{SECOND_INSTANCE_ID}",
        ),
    ),
)
def test_malformed_stored_generation_metadata_fails_closed(
    persistence_instance_id: str | None,
    persistence_namespace: str | None,
) -> None:
    redis = _ServerTimeLeaseRedis(server_time=1000)
    redis.records[ACCOUNT_A_NAMESPACE] = {
        "namespace": ACCOUNT_A_NAMESPACE,
        "owner": "node-a:process-1",
        "release_id": "release-a",
        "redis_fencing_epoch": FIRST_REDIS_FENCING_EPOCH,
        "fencing_token": 7,
        "persistence_instance_id": persistence_instance_id,
        "persistence_namespace": persistence_namespace,
        "refreshed_at_epoch": 1000,
    }

    with pytest.raises(
        RedisNamespaceLeaseError,
        match="metadata is invalid",
    ):
        lease_module.get_namespace_lease(redis, ACCOUNT_A_NAMESPACE)


def test_unicode_fencing_token_metadata_fails_closed_like_lua() -> None:
    redis = _ServerTimeLeaseRedis(server_time=1000)
    redis.records[ACCOUNT_A_NAMESPACE] = {
        "namespace": ACCOUNT_A_NAMESPACE,
        "owner": "node-a:process-1",
        "release_id": "release-a",
        "redis_fencing_epoch": FIRST_REDIS_FENCING_EPOCH,
        "fencing_token": "\N{ARABIC-INDIC DIGIT ONE}",
        "refreshed_at_epoch": 1000,
    }

    with pytest.raises(
        RedisNamespaceLeaseError,
        match="metadata is invalid",
    ):
        lease_module.get_namespace_lease(redis, ACCOUNT_A_NAMESPACE)


@pytest.mark.parametrize(
    "metadata_epoch",
    (
        None,
        "",
        "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
        "aaaaaaaa-aaaa-1aaa-8aaa-aaaaaaaaaaaa",
        "not-a-uuid",
    ),
)
def test_epoch_metadata_missing_or_corrupt_fails_closed(
    metadata_epoch: str | None,
) -> None:
    redis = _ServerTimeLeaseRedis(server_time=1000)
    redis.records[ACCOUNT_A_NAMESPACE] = {
        "namespace": ACCOUNT_A_NAMESPACE,
        "owner": "node-a:process-1",
        "release_id": "release-a",
        "fencing_token": 7,
        "persistence_instance_id": FIRST_INSTANCE_ID,
        "persistence_namespace": (
            f"{ACCOUNT_A_NAMESPACE}:{FIRST_INSTANCE_ID}"
        ),
        "refreshed_at_epoch": 1000,
    }
    if metadata_epoch is not None:
        redis.records[ACCOUNT_A_NAMESPACE]["redis_fencing_epoch"] = (
            metadata_epoch
        )

    with pytest.raises(
        RedisNamespaceLeaseError,
        match="metadata is invalid",
    ):
        lease_module.get_namespace_lease(redis, ACCOUNT_A_NAMESPACE)


def test_acquire_rejects_fencing_token_above_48_bit_limit() -> None:
    redis = _StaticEvalRedis(
        [
            1,
            lease_module.MAX_PERSISTENCE_GENERATION_FENCING_TOKEN + 1,
            1000,
            FIRST_INSTANCE_ID,
            f"{ACCOUNT_A_NAMESPACE}:{FIRST_INSTANCE_ID}",
            FIRST_REDIS_FENCING_EPOCH,
        ]
    )

    with pytest.raises(
        RedisNamespaceLeaseError,
        match="generation limit",
    ):
        lease_module.acquire_namespace_lease(
            redis,
            namespace=ACCOUNT_A_NAMESPACE,
            owner="node-a:process-1",
            release_id="release-a",
            persistence_instance_id_candidate=FIRST_INSTANCE_ID,
        )


@pytest.mark.parametrize(
    "candidate",
    (
        "11111111-1111-1111-8111-111111111111",
        "11111111-1111-4111-7111-111111111111",
        "11111111-1111-4111-8111-11111111111A",
    ),
)
def test_acquire_rejects_non_canonical_uuid4_candidate(candidate: str) -> None:
    redis = _ServerTimeLeaseRedis()
    lease = RedisNamespaceLease(
        redis,
        namespace=ACCOUNT_A_NAMESPACE,
        owner="node-a:process-1",
        release_id="release-a",
        persistence_instance_id_factory=_CandidateFactory(candidate),
    )

    with pytest.raises(
        RedisNamespaceLeaseError,
        match="canonical UUID4",
    ):
        lease.acquire()

    assert redis.eval_calls == []


def test_lua_contract_uses_redis_time_without_client_epoch_arguments() -> None:
    assert 'redis.call("TIME")' in lease_module._ACQUIRE_LUA
    assert 'redis.call("TIME")' in lease_module._REFRESH_LUA
    assert "refreshed_at_epoch = tonumber(ARGV" not in lease_module._ACQUIRE_LUA
    assert "refreshed_at_epoch = tonumber(ARGV" not in lease_module._REFRESH_LUA


def test_lua_contract_reads_fixed_epoch_marker_inside_every_mutation() -> None:
    scripts = (
        lease_module._ACQUIRE_LUA,
        lease_module._REFRESH_LUA,
        lease_module._REMOVE_LUA,
        lease_module._FORCE_REMOVE_STALE_LUA,
    )
    for script in scripts:
        assert 'redis.call("GET", epoch_key)' in script
        assert "is_canonical_uuid4(redis_fencing_epoch)" in script

    assert "-- redis-namespace-lease:acquire-v7" in lease_module._ACQUIRE_LUA
    assert "-- redis-namespace-lease:refresh-v5" in lease_module._REFRESH_LUA
    assert "-- redis-namespace-lease:remove-v4" in lease_module._REMOVE_LUA
    assert (
        "-- redis-namespace-lease:force-remove-stale-v3"
        in lease_module._FORCE_REMOVE_STALE_LUA
    )


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


def test_lua_contract_validates_stored_tokens_and_counter_exactly() -> None:
    scripts = (
        lease_module._ACQUIRE_LUA,
        lease_module._REFRESH_LUA,
        lease_module._REMOVE_LUA,
        lease_module._FORCE_REMOVE_STALE_LUA,
    )
    for script in scripts:
        assert "local function decode_fencing_token" in script
        compact_script = "".join(script.split())
        assert (
            'decode_fencing_token(current["fencing_token"])'
            in compact_script
        )

    assert 'string.match(raw_counter, "^[0-9]+$")' in lease_module._ACQUIRE_LUA
