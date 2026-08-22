from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    REPO_ROOT
    / "scripts"
    / "jp24_redis_dead_instance_janitor.py"
)
SERVICE = (
    REPO_ROOT
    / "infra"
    / "systemd"
    / "trader-v3-redis-dead-instance-janitor.service"
)
TIMER = (
    REPO_ROOT
    / "infra"
    / "systemd"
    / "trader-v3-redis-dead-instance-janitor.timer"
)
LIVE_INSTANCE_ID = "11111111-1111-4111-8111-111111111111"
DEAD_INSTANCE_ID = "22222222-2222-4222-8222-222222222222"


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "jp24_redis_dead_instance_janitor",
        SCRIPT,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeRedis:
    def __init__(self) -> None:
        self.server_epoch = 10_000
        self.used_memory = 750
        self.maxmemory = 1000
        self.last_save_epoch = 9_000
        self.bgsave_calls = 0
        self.bgsave_polls_remaining = 0
        self.lease_namespace = "trader-TRADER-ACCOUNT-A"
        self.lease_record = {
            "namespace": self.lease_namespace,
            "persistence_instance_id": LIVE_INSTANCE_ID,
            "persistence_namespace": (
                f"{self.lease_namespace}:{LIVE_INSTANCE_ID}"
            ),
            "refreshed_at_epoch": self.server_epoch - 5,
        }
        self.keys = {
            (
                f"{self.lease_namespace}:{LIVE_INSTANCE_ID}:"
                "nautilus:cache:live"
            ),
            (
                f"{self.lease_namespace}:{DEAD_INSTANCE_ID}:"
                "nautilus:cache:dead"
            ),
            "trader-bot:redis-namespaces:active",
            "trader-bot:redis-namespaces:active:leases",
            "trader-TRADER-ACCOUNT-A:not-a-uuid:nautilus:cache",
            "unrelated:key",
        }

    def info(self, section: str):
        if section == "memory":
            return {
                "used_memory": self.used_memory,
                "maxmemory": self.maxmemory,
            }
        assert section == "persistence"
        if self.bgsave_polls_remaining > 0:
            self.bgsave_polls_remaining -= 1
            return {
                "rdb_bgsave_in_progress": 1,
                "rdb_last_bgsave_status": "ok",
                "rdb_last_save_time": self.last_save_epoch,
            }
        if self.bgsave_calls:
            self.last_save_epoch = self.server_epoch
        return {
            "rdb_bgsave_in_progress": 0,
            "rdb_last_bgsave_status": "ok",
            "rdb_last_save_time": self.last_save_epoch,
        }

    def bgsave(self):
        self.bgsave_calls += 1
        self.bgsave_polls_remaining = 1
        return True

    def hgetall(self, key: str):
        assert key.endswith(":leases")
        return {
            self.lease_namespace: json.dumps(
                self.lease_record,
                sort_keys=True,
            )
        }

    def zscore(self, key: str, member: str):
        assert key == "trader-bot:redis-namespaces:active"
        if member != self.lease_namespace:
            return None
        return float(self.lease_record["refreshed_at_epoch"])

    def time(self):
        return self.server_epoch, 0

    def scan_iter(self, match: str, count: int):
        assert match == "trader-TRADER-ACCOUNT-*:*:*"
        assert count > 0
        return iter(sorted(self.keys))

    def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: object,
    ):
        assert "jp24-redis-dead-instance-janitor:delete-v1" in script
        assert numkeys == 3
        candidate = str(keys_and_args[2])
        instance_id = str(keys_and_args[3])
        if instance_id == LIVE_INSTANCE_ID:
            return [0, "PROTECTED"]
        deleted = int(candidate in self.keys)
        self.keys.discard(candidate)
        return [deleted, "DELETED"]


class ImmediateSameSecondBgsaveRedis(FakeRedis):
    def info(self, section: str):
        if section == "memory":
            return super().info(section)
        assert section == "persistence"
        return {
            "rdb_bgsave_in_progress": 0,
            "rdb_last_bgsave_status": "ok",
            "rdb_last_save_time": self.last_save_epoch,
        }

    def bgsave(self):
        self.bgsave_calls += 1
        return True


def test_dry_run_outputs_live_whitelist_and_dead_delete_list() -> None:
    module = _load_script()
    redis = FakeRedis()

    report = module.run_janitor(
        redis,
        dry_run=True,
        reasons=("forced",),
        sleep_fn=lambda _seconds: None,
    )

    assert redis.bgsave_calls == 1
    assert report.whitelist_instance_ids == (LIVE_INSTANCE_ID,)
    assert report.delete_keys == (
        (
            "trader-TRADER-ACCOUNT-A:"
            f"{DEAD_INSTANCE_ID}:nautilus:cache:dead"
        ),
    )
    assert report.deleted_count == 0
    assert report.memory_before.used_memory == 750
    assert report.memory_after.used_memory == 750
    assert (
        "trader-bot:redis-namespaces:active:leases"
        not in report.delete_keys
    )


def test_bgsave_same_second_completion_requires_elapsed_confirmation() -> None:
    module = _load_script()
    redis = ImmediateSameSecondBgsaveRedis()
    monotonic = 0.0
    sleeps = []

    def monotonic_fn() -> float:
        nonlocal monotonic
        current = monotonic
        monotonic += 0.5
        return current

    receipt = module._complete_bgsave(
        redis,
        timeout_seconds=10,
        sleep_fn=sleeps.append,
        monotonic_fn=monotonic_fn,
    )

    assert redis.bgsave_calls == 1
    assert receipt.completed_at_epoch == 9_000
    assert sleeps


def test_apply_deletes_only_non_whitelisted_uuid_keys() -> None:
    module = _load_script()
    redis = FakeRedis()

    report = module.run_janitor(
        redis,
        dry_run=False,
        reasons=("memory",),
        sleep_fn=lambda _seconds: None,
    )

    live_key = (
        "trader-TRADER-ACCOUNT-A:"
        f"{LIVE_INSTANCE_ID}:nautilus:cache:live"
    )
    dead_key = (
        "trader-TRADER-ACCOUNT-A:"
        f"{DEAD_INSTANCE_ID}:nautilus:cache:dead"
    )
    assert report.deleted_count == 1
    assert live_key in redis.keys
    assert dead_key not in redis.keys
    assert "trader-bot:redis-namespaces:active" in redis.keys
    assert "trader-bot:redis-namespaces:active:leases" in redis.keys


def test_memory_above_seventy_percent_triggers_between_daily_runs() -> None:
    module = _load_script()
    memory = module.MemorySnapshot(
        used_memory=701,
        maxmemory=1000,
        used_ratio=0.701,
    )

    reasons = module.trigger_reasons(
        now_epoch=20_000,
        last_success_epoch=19_900,
        memory=memory,
        daily_interval_seconds=86_400,
        memory_threshold_ratio=0.70,
        force=False,
    )

    assert reasons == ("memory",)


def test_systemd_timer_checks_frequently_and_service_runs_apply_mode() -> None:
    service = SERVICE.read_text(encoding="utf-8")
    timer = TIMER.read_text(encoding="utf-8")

    assert "jp24_redis_dead_instance_janitor.py --apply" in service
    assert "EnvironmentFile=/etc/trader-v3/redis-janitor.env" in service
    assert "StateDirectory=trader-v3" in service
    assert "OnUnitActiveSec=5min" in timer
    assert "Persistent=true" in timer
    assert (
        "Unit=trader-v3-redis-dead-instance-janitor.service"
        in timer
    )
