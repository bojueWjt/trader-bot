from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "refresh_redis_capacity_evidence.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "refresh_redis_capacity_evidence",
        SCRIPT,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


refresh = _load_module()


def _base_evidence() -> dict:
    gib = 1024**3
    return {
        "schema_version": "trader-v3-redis-capacity-evidence/v3",
        "active_container": "trader-v3-redis",
        "active_container_id": "a" * 64,
        "active_redis_run_id": "b" * 40,
        "active_volume": "trader-v3-redis-data-v2",
        "active_volume_source": "/var/lib/docker/volumes/redis-v2/_data",
        "redis_fencing_epoch": "8e392566-3f8e-46c2-a038-86b8ff8b21b4",
        "redis_fencing_epoch_key": "trader-bot:redis-fencing-epoch",
        "redis_fencing_epoch_sha256": hashlib.sha256(
            b"8e392566-3f8e-46c2-a038-86b8ff8b21b4"
        ).hexdigest(),
        "maxmemory_bytes": gib,
        "headroom_percent": 20,
        "container_headroom_percent": 20,
        "redis_cgroup_limit_bytes": 2 * gib,
        "memory_limit_bytes": 2 * gib,
        "memory_swap_limit_bytes": 2 * gib,
        "other_services_reserve_bytes": gib,
        "system_reserve_bytes": 3 * gib,
        "maxmemory_policy": "noeviction",
        "active_appendonly": "no",
        "active_aof_enabled": 0,
        "active_save_policy": "3600 1",
        "active_rdb_last_bgsave_status": "ok",
    }


def _inspect() -> dict:
    gib = 1024**3
    return {
        "Id": "a" * 64,
        "Image": "sha256:" + "c" * 64,
        "HostConfig": {
            "Memory": 2 * gib,
            "MemorySwap": 2 * gib,
        },
        "Mounts": [
            {
                "Type": "volume",
                "Name": "trader-v3-redis-data-v2",
                "Source": "/var/lib/docker/volumes/redis-v2/_data",
                "Destination": "/data",
                "RW": True,
            }
        ],
    }


def _runtime() -> dict:
    return {
        "run_id": "b" * 40,
        "dbsize": 1,
        "fencing_epoch": "8e392566-3f8e-46c2-a038-86b8ff8b21b4",
        "maxmemory_bytes": 1024**3,
        "maxmemory_policy": "noeviction",
        "used_memory_bytes": 128 * 1024**2,
        "dataset_memory_bytes": 64 * 1024**2,
        "appendonly": "no",
        "aof_enabled": 0,
        "save_policy": "3600 1",
        "rdb_last_bgsave_status": "ok",
    }


def test_refresh_rejects_live_container_identity_drift(tmp_path: Path) -> None:
    base_path = tmp_path / "capacity-evidence.json"
    base = _base_evidence()
    base_path.write_text(json.dumps(base), encoding="utf-8")
    inspect = _inspect()
    inspect["Id"] = "d" * 64

    with pytest.raises(
        refresh.CapacityRefreshError,
        match="container ID differs",
    ):
        refresh.build_refresh_receipt(
            base_path=base_path,
            base=base,
            inspect=inspect,
            runtime=_runtime(),
            host_total_bytes=16 * 1024**3,
            host_available_bytes=12 * 1024**3,
            disk_free_bytes=20 * 1024**3,
            minimum_disk_free_bytes=8 * 1024**3,
            now=datetime(2026, 8, 18, tzinfo=timezone.utc),
        )


def test_verify_rejects_receipt_after_base_evidence_changes(
    tmp_path: Path,
) -> None:
    base_path = tmp_path / "capacity-evidence.json"
    receipt_path = tmp_path / "capacity-refresh.json"
    base = _base_evidence()
    base_path.write_text(
        json.dumps(base, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    receipt = refresh.build_refresh_receipt(
        base_path=base_path,
        base=base,
        inspect=_inspect(),
        runtime=_runtime(),
        host_total_bytes=16 * 1024**3,
        host_available_bytes=12 * 1024**3,
        disk_free_bytes=20 * 1024**3,
        minimum_disk_free_bytes=8 * 1024**3,
        now=datetime(2026, 8, 18, tzinfo=timezone.utc),
    )
    refresh.write_receipt_atomic(receipt_path, receipt)
    base["active_volume"] = "drifted-volume"
    base_path.write_text(json.dumps(base), encoding="utf-8")

    with pytest.raises(
        refresh.CapacityRefreshError,
        match="base evidence hash differs",
    ):
        refresh.verify_refresh_receipt(
            receipt_path=receipt_path,
            base_path=base_path,
            now=datetime(2026, 8, 18, 0, 5, tzinfo=timezone.utc),
            max_age_seconds=86400,
        )


@pytest.mark.parametrize(
    ("host_available_bytes", "disk_free_bytes", "message"),
    [
        (2 * 1024**3, 20 * 1024**3, "host available memory"),
        (12 * 1024**3, 4 * 1024**3, "disk free space"),
    ],
)
def test_refresh_rejects_insufficient_live_reserve(
    tmp_path: Path,
    host_available_bytes: int,
    disk_free_bytes: int,
    message: str,
) -> None:
    base_path = tmp_path / "capacity-evidence.json"
    base = _base_evidence()
    base_path.write_text(json.dumps(base), encoding="utf-8")

    with pytest.raises(refresh.CapacityRefreshError, match=message):
        refresh.build_refresh_receipt(
            base_path=base_path,
            base=base,
            inspect=_inspect(),
            runtime=_runtime(),
            host_total_bytes=16 * 1024**3,
            host_available_bytes=host_available_bytes,
            disk_free_bytes=disk_free_bytes,
            minimum_disk_free_bytes=8 * 1024**3,
            now=datetime(2026, 8, 18, tzinfo=timezone.utc),
        )


def test_receipt_is_atomically_replaced_and_fresh(tmp_path: Path) -> None:
    base_path = tmp_path / "capacity-evidence.json"
    receipt_path = tmp_path / "capacity-refresh.json"
    base = _base_evidence()
    base_path.write_text(
        json.dumps(base, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    receipt = refresh.build_refresh_receipt(
        base_path=base_path,
        base=base,
        inspect=_inspect(),
        runtime=_runtime(),
        host_total_bytes=16 * 1024**3,
        host_available_bytes=12 * 1024**3,
        disk_free_bytes=20 * 1024**3,
        minimum_disk_free_bytes=8 * 1024**3,
        now=now,
    )

    refresh.write_receipt_atomic(receipt_path, receipt)
    refresh.verify_refresh_receipt(
        receipt_path=receipt_path,
        base_path=base_path,
        now=now + timedelta(hours=23),
        max_age_seconds=86400,
    )

    assert receipt_path.stat().st_mode & 0o777 == 0o400
    assert list(tmp_path.glob(".capacity-refresh.json.*")) == []
