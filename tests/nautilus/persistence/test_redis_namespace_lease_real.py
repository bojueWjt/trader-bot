from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
sys.path.insert(0, str(SERVICE_ROOT))

from persistence.redis_namespace_lease import (
    DEFAULT_REDIS_FENCING_EPOCH_KEY,
    RedisNamespaceLease,
    RedisNamespaceLeaseError,
    RedisNamespaceLeaseLost,
    RedisNamespaceLeaseRecord,
    force_remove_stale_namespace_lease,
    list_active_namespace_leases,
    refresh_namespace_lease,
)
from persistence.redis_resp_client import RedisRespClient

REDIS_INTEGRATION_URL = os.environ.get("REDIS_INTEGRATION_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not REDIS_INTEGRATION_URL,
    reason="REDIS_INTEGRATION_URL is required for real Redis tests",
)

_UUID4_A = "11111111-1111-4111-8111-111111111111"
_UUID4_B = "22222222-2222-4222-8222-222222222222"
_REDIS_FENCING_EPOCH = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


@dataclass
class RealRedisFixture:
    client: RedisRespClient
    registry_key: str

    @property
    def metadata_key(self) -> str:
        return f"{self.registry_key}:leases"

    @property
    def fencing_key(self) -> str:
        return f"{self.registry_key}:fencing"

    def store(self, namespace: str, payload: dict[str, object]) -> None:
        refreshed_at_epoch = int(payload["refreshed_at_epoch"])
        self.client.execute(
            "HSET",
            self.metadata_key,
            namespace,
            json.dumps(payload, sort_keys=True),
        )
        self.client.execute(
            "ZADD",
            self.registry_key,
            refreshed_at_epoch,
            namespace,
        )

    def close(self) -> None:
        try:
            self.client.execute(
                "DEL",
                self.registry_key,
                self.metadata_key,
                self.fencing_key,
            )
        finally:
            self.client.close()


@pytest.fixture
def real_redis() -> RealRedisFixture:
    fixture = RealRedisFixture(
        client=RedisRespClient(REDIS_INTEGRATION_URL),
        registry_key=f"trader-bot:test:namespace-lease:{uuid4().hex}",
    )
    fixture.client.execute("PING")
    previous_epoch = fixture.client.execute(
        "GET",
        DEFAULT_REDIS_FENCING_EPOCH_KEY,
    )
    fixture.client.execute(
        "SET",
        DEFAULT_REDIS_FENCING_EPOCH_KEY,
        _REDIS_FENCING_EPOCH,
    )
    try:
        yield fixture
    finally:
        try:
            if previous_epoch is None:
                fixture.client.execute(
                    "DEL",
                    DEFAULT_REDIS_FENCING_EPOCH_KEY,
                )
            else:
                fixture.client.execute(
                    "SET",
                    DEFAULT_REDIS_FENCING_EPOCH_KEY,
                    previous_epoch,
                )
        finally:
            fixture.close()


@pytest.mark.parametrize(
    "generation_fields",
    (
        {
            "persistence_instance_id": None,
            "persistence_namespace": None,
        },
        {
            "persistence_instance_id": False,
            "persistence_namespace": False,
        },
        {
            "persistence_instance_id": "invalid",
            "persistence_namespace": (
                "trader-TRADER-ACCOUNT-A:invalid"
            ),
        },
        {
            "persistence_instance_id": _UUID4_A,
            "persistence_namespace": (
                f"trader-TRADER-ACCOUNT-A:{_UUID4_B}"
            ),
        },
    ),
)
def test_real_redis_acquire_rejects_corrupt_generation_metadata(
    real_redis: RealRedisFixture,
    generation_fields: dict[str, object],
) -> None:
    namespace = "trader-TRADER-ACCOUNT-A"
    server_time = real_redis.client.time()[0]
    real_redis.store(
        namespace,
        {
            "namespace": namespace,
            "owner": "node-a",
            "release_id": "release-a",
            "redis_fencing_epoch": _REDIS_FENCING_EPOCH,
            "fencing_token": 7,
            "refreshed_at_epoch": server_time,
            **generation_fields,
        },
    )

    with pytest.raises(RedisNamespaceLeaseLost, match="corrupt"):
        RedisNamespaceLease(
            real_redis.client,
            namespace=namespace,
            owner="node-a",
            release_id="release-a",
            registry_key=real_redis.registry_key,
            persistence_instance_id_factory=lambda: _UUID4_B,
        ).acquire()


def test_real_redis_rejects_false_generation_for_all_mutations(
    real_redis: RealRedisFixture,
) -> None:
    namespace = "trader-TRADER-ACCOUNT-A"
    lease = RedisNamespaceLease(
        real_redis.client,
        namespace=namespace,
        owner="node-a",
        release_id="release-a",
        registry_key=real_redis.registry_key,
        persistence_instance_id_factory=lambda: _UUID4_A,
    )
    record = lease.acquire()
    real_redis.store(
        namespace,
        {
            "namespace": namespace,
            "owner": record.owner,
            "release_id": record.release_id,
            "fencing_token": record.fencing_token,
            "persistence_instance_id": False,
            "persistence_namespace": False,
            "refreshed_at_epoch": record.refreshed_at_epoch,
        },
    )

    with pytest.raises(RedisNamespaceLeaseLost, match="corrupt"):
        RedisNamespaceLease(
            real_redis.client,
            namespace=namespace,
            owner=record.owner,
            release_id=record.release_id,
            registry_key=real_redis.registry_key,
            persistence_instance_id_factory=lambda: _UUID4_B,
        ).acquire()
    with pytest.raises(RedisNamespaceLeaseLost, match="corrupt"):
        lease.refresh()

    assert lease.release() is False
    with pytest.raises(RedisNamespaceLeaseError, match="corrupt"):
        force_remove_stale_namespace_lease(
            real_redis.client,
            namespace,
            registry_key=real_redis.registry_key,
        )
    assert real_redis.client.hget(real_redis.metadata_key, namespace) is not None


@pytest.mark.parametrize(
    "generation_fields",
    (
        {"persistence_instance_id": _UUID4_A},
        {
            "persistence_namespace": (
                "trader-TRADER-ACCOUNT-A:"
                "11111111-1111-4111-8111-111111111111"
            )
        },
    ),
)
def test_real_redis_repeat_acquire_backfills_single_generation_field(
    real_redis: RealRedisFixture,
    generation_fields: dict[str, object],
) -> None:
    namespace = "trader-TRADER-ACCOUNT-A"
    server_time = real_redis.client.time()[0]
    real_redis.store(
        namespace,
        {
            "namespace": namespace,
            "owner": "node-a",
            "release_id": "release-a",
            "redis_fencing_epoch": _REDIS_FENCING_EPOCH,
            "fencing_token": 7,
            "refreshed_at_epoch": server_time,
            **generation_fields,
        },
    )

    acquired = RedisNamespaceLease(
        real_redis.client,
        namespace=namespace,
        owner="node-a",
        release_id="release-a",
        registry_key=real_redis.registry_key,
        persistence_instance_id_factory=lambda: _UUID4_B,
    ).acquire()
    raw_record = real_redis.client.hget(real_redis.metadata_key, namespace)

    assert raw_record is not None
    persisted = json.loads(_decode(raw_record))
    assert acquired.fencing_token == 7
    assert acquired.persistence_instance_id == _UUID4_A
    assert persisted["persistence_instance_id"] == _UUID4_A
    assert persisted["persistence_namespace"] == f"{namespace}:{_UUID4_A}"


def test_real_redis_list_retries_across_refresh_snapshot(
    real_redis: RealRedisFixture,
) -> None:
    namespace = "trader-TRADER-ACCOUNT-A"
    acquired = RedisNamespaceLease(
        real_redis.client,
        namespace=namespace,
        owner="node-a",
        release_id="release-a",
        registry_key=real_redis.registry_key,
        persistence_instance_id_factory=lambda: _UUID4_A,
    ).acquire()
    _wait_for_next_redis_second(real_redis.client, acquired.refreshed_at_epoch)
    writer = RedisRespClient(REDIS_INTEGRATION_URL)
    interleaving = _RefreshAfterFirstMetadataRead(
        reader=real_redis.client,
        writer=writer,
        record=acquired,
        registry_key=real_redis.registry_key,
    )
    try:
        records = list_active_namespace_leases(
            interleaving,
            fresh_after_epoch=acquired.refreshed_at_epoch,
            registry_key=real_redis.registry_key,
        )
    finally:
        writer.close()

    assert len(records) == 1
    assert interleaving.refreshed is not False
    assert records[0] == interleaving.refreshed
    assert interleaving.score_reads == 2


class _RefreshAfterFirstMetadataRead:
    def __init__(
        self,
        *,
        reader: RedisRespClient,
        writer: RedisRespClient,
        record: RedisNamespaceLeaseRecord,
        registry_key: str,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._record = record
        self._registry_key = registry_key
        self._triggered = False
        self.refreshed: RedisNamespaceLeaseRecord | bool = False
        self.score_reads = 0

    def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: object,
    ) -> object:
        return self._reader.eval(script, numkeys, *keys_and_args)

    def zrangebyscore(
        self,
        key: str,
        minimum: int,
        maximum: str,
        *,
        withscores: bool,
    ):
        return self._reader.zrangebyscore(
            key,
            minimum,
            maximum,
            withscores=withscores,
        )

    def hget(self, key: str, field: str) -> bytes | str | None:
        raw_record = self._reader.hget(key, field)
        if not self._triggered:
            self._triggered = True
            self.refreshed = refresh_namespace_lease(
                self._writer,
                record=self._record,
                registry_key=self._registry_key,
            )
        return raw_record

    def zscore(self, key: str, member: str) -> float | None:
        self.score_reads += 1
        return self._reader.zscore(key, member)


def _wait_for_next_redis_second(
    client: RedisRespClient,
    previous_epoch: int,
) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if client.time()[0] > previous_epoch:
            return
        time.sleep(0.02)
    raise AssertionError("Redis TIME did not advance")


def _decode(value: bytes | str) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value
