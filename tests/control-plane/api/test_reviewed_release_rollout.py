from __future__ import annotations

import json
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import psycopg2
import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import reviewed_release_rollout as rollout
import release_manifest


RELEASE_ID = "a" * 64
IMAGE_DIGEST = "sha256:" + ("1" * 64)
CONFIG_SHA256 = "2" * 64
LOCK_SHA256 = "3" * 64
SCHEMA_EPOCH = "0015_refresh_evidence_command"
MANIFEST_SHA256 = "4" * 64
BUNDLE_SHA256 = "5" * 64
RELEASE_ROOT_PATH = "/srv/trader-v3/releases/reviewed-release-a"
RELEASE_SOURCE_MANIFEST_SHA256 = "8" * 64
LIVE_ADAPTER_SHA256 = "9" * 64
REDIS_FENCING_EPOCH = "123e4567-e89b-42d3-a456-426614174000"
PREVIOUS_REDIS_FENCING_EPOCH = "123e4567-e89b-42d3-8456-426614174099"
REPLACEMENT_REDIS_FENCING_EPOCH = (
    "123e4567-e89b-42d3-b456-426614174001"
)
REDIS_FENCING_EPOCH_KEY = "trader-bot:redis-fencing-epoch"
OLD_CONFIG_SHA256 = "6" * 64
OLD_LOCK_SHA256 = "7" * 64
OLD_SCHEMA_EPOCH = "0009_trade_outcome_job_runs"


class _HeldOperationLock:
    owner_token = "a" * 64

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return

    def require_held(self) -> None:
        return


OPERATION_LOCK = _HeldOperationLock()


@pytest.fixture(autouse=True)
def _stub_mutation_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rollout,
        "_require_maintenance_fence",
        lambda *_args, stage, **_kwargs: {
            "fence_id": REDIS_FENCING_EPOCH,
            "operation": "deploy",
            "actor": "pytest",
            "lease_version": 1,
            "expires_at": "2099-01-01T00:00:00+00:00",
            "last_stage": "pytest",
            "rollout_stage": stage,
        },
    )
    monkeypatch.setattr(
        rollout,
        "_lock_registration_heartbeats",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        rollout,
        "_active_redis_fencing_epoch",
        lambda *_args, **_kwargs: PREVIOUS_REDIS_FENCING_EPOCH,
    )
    monkeypatch.setattr(
        rollout,
        "load_live_canary_closure_report",
        lambda report_path, *_args: _closure_report(
            Path(report_path).stem
        ),
    )


def _write_node_configs(root: Path) -> list[dict]:
    specs = {}
    for suffix in ("a", "b", "c", "d"):
        account_id = f"account-{suffix}"
        config = {
            "account_id": account_id,
            "node_id": f"nautilus-node-{account_id}",
            "trader_id": f"trader-{account_id}",
            "instance_id": f"instance-{account_id}",
            "runtime_resources": {
                "schema_version": (
                    release_manifest.RUNTIME_RESOURCES_SCHEMA_VERSION
                ),
                "redis": {
                    "stream_max_entries": 1000,
                    "stream_max_bytes": 1_048_576,
                    "total_stream_max_bytes": 8_388_608,
                    "scan_count": 100,
                    "sample_interval_seconds": 1.0,
                    "critical_window_seconds": 30.0,
                    "thread_join_timeout_seconds": 5.0,
                    "memory_warning_ratio": 0.6,
                    "memory_degraded_ratio": 0.75,
                    "memory_critical_ratio": 0.85,
                },
                "command_journal": {
                    "max_bytes": 1_048_576,
                },
            },
        }
        payload = (
            json.dumps(config, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        digest = sha256(payload).hexdigest()
        config_root = root / "config-artifacts" / account_id
        config_root.mkdir(parents=True)
        config_path = config_root / f"{digest}.json"
        config_path.write_bytes(payload)
        config_path.chmod(0o440)
        specs[account_id] = (
            f"trader-v3-node-{suffix}",
            config_path,
        )
    return release_manifest.build_node_config_artifacts(specs)


def _document() -> rollout.ReleaseDocument:
    return rollout.ReleaseDocument(
        release_id=RELEASE_ID,
        image_digest=IMAGE_DIGEST,
        config_sha256=CONFIG_SHA256,
        dependency_lock_sha256=LOCK_SHA256,
        schema_epoch=SCHEMA_EPOCH,
        manifest_sha256=MANIFEST_SHA256,
        bundle_manifest_sha256=BUNDLE_SHA256,
        delivery_mode="immutable_image",
        release_root_path=RELEASE_ROOT_PATH,
        release_source_manifest_sha256=RELEASE_SOURCE_MANIFEST_SHA256,
        live_adapter_sha256=LIVE_ADAPTER_SHA256,
        node_ids=tuple(sorted(rollout.EXPECTED_NODE_IDS.items())),
    )


def _write_release_material(root: Path) -> tuple[Path, Path]:
    payload = root / "node.py"
    payload.write_bytes(b"reviewed immutable runtime")
    bundle = {
        "repo_commit": "5" * 40,
        "repo_dirty": False,
        "schema_epochs": dict(release_manifest.SCHEMA_EPOCHS),
        "files": [
            {
                "bundle_path": payload.name,
                "mount_target": "/app/app/node.py",
                "sha256": release_manifest.sha256_file(payload),
            }
        ],
    }
    bundle_path = root / "bundle-manifest.json"
    bundle_path.write_text(
        json.dumps(bundle, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest = release_manifest.build_release_manifest(
        bundle,
        image_digest=IMAGE_DIGEST,
        config_sha256=None,
        dependency_lock_sha256=LOCK_SHA256,
        patch_root=root,
        delivery_mode=release_manifest.DELIVERY_IMMUTABLE,
        node_configs=_write_node_configs(root),
    )
    manifest_path = root / "release-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest_path, bundle_path


def _write_capacity_evidence(
    root: Path,
    *,
    redis_fencing_epoch: str = REDIS_FENCING_EPOCH,
    active_run_id: str = "a" * 40,
    active_volume: str = "trader-v3-redis-hardening-test",
) -> Path:
    evidence = {
        "schema_version": "trader-v3-redis-capacity-evidence/v3",
        "passed": True,
        "nodes_stopped": True,
        "dataset_mode": "empty-volume-exchange-first-rebaseline",
        "active_container": "trader-v3-redis",
        "active_redis_run_id": active_run_id,
        "initial_redis_run_id": active_run_id,
        "active_volume": active_volume,
        "active_volume_source": f"/var/lib/docker/volumes/{active_volume}/_data",
        "active_key_count": 1,
        "redis_fencing_epoch": redis_fencing_epoch,
        "redis_fencing_epoch_key": REDIS_FENCING_EPOCH_KEY,
        "redis_fencing_epoch_sha256": sha256(
            redis_fencing_epoch.encode("ascii")
        ).hexdigest(),
        "control_keys_reinitialized": True,
    }
    path = root / f"capacity-{redis_fencing_epoch}.json"
    path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _seed_heartbeat(
    url: str,
    *,
    account_id: str,
    node_id: str | None = None,
    release_id: str = RELEASE_ID,
    image_digest: str = IMAGE_DIGEST,
    config_sha256: str = CONFIG_SHA256,
    dependency_lock_sha256: str = LOCK_SHA256,
    schema_epoch: str = SCHEMA_EPOCH,
    status: str = "HALTED",
    redis_fencing_epoch: str = REDIS_FENCING_EPOCH,
) -> None:
    node_id = node_id or rollout.EXPECTED_NODE_IDS[account_id]
    with psycopg2.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO node_heartbeats (
                node_id,
                account_id,
                status,
                release_id,
                image_digest,
                config_sha256,
                dependency_lock_sha256,
                schema_epoch,
                redis_fencing_epoch,
                runtime_generation,
                lease_fencing_token,
                heartbeat_sequence,
                last_seen_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, 41, 1, now()
            )
            ON CONFLICT (node_id) DO UPDATE
            SET account_id=EXCLUDED.account_id,
                status=EXCLUDED.status,
                release_id=EXCLUDED.release_id,
                image_digest=EXCLUDED.image_digest,
                config_sha256=EXCLUDED.config_sha256,
                dependency_lock_sha256=EXCLUDED.dependency_lock_sha256,
                schema_epoch=EXCLUDED.schema_epoch,
                redis_fencing_epoch=EXCLUDED.redis_fencing_epoch,
                runtime_generation=EXCLUDED.runtime_generation,
                lease_fencing_token=EXCLUDED.lease_fencing_token,
                heartbeat_sequence=(
                    node_heartbeats.heartbeat_sequence + 1
                ),
                last_seen_at=now()
            """,
            (
                node_id,
                account_id,
                status,
                release_id,
                image_digest,
                config_sha256,
                dependency_lock_sha256,
                schema_epoch,
                redis_fencing_epoch,
                f"runtime-{account_id}",
            ),
        )


def _seed_approved_old_release(
    url: str,
    account_id: str,
) -> None:
    old_release_id = f"old-release-{account_id}"
    old_image_digest = "sha256:" + (account_id[-1] * 64)
    with psycopg2.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO reviewed_release_manifests (
                account_id,
                release_id,
                image_digest,
                config_sha256,
                dependency_lock_sha256,
                schema_epoch,
                review_status,
                reviewed_by,
                reviewed_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, 'reviewed', 'pytest', now())
            """,
            (
                account_id,
                old_release_id,
                old_image_digest,
                OLD_CONFIG_SHA256,
                OLD_LOCK_SHA256,
                OLD_SCHEMA_EPOCH,
            ),
        )
    _seed_heartbeat(
        url,
        account_id=account_id,
        release_id=old_release_id,
        image_digest=old_image_digest,
        config_sha256=OLD_CONFIG_SHA256,
        dependency_lock_sha256=OLD_LOCK_SHA256,
        schema_epoch=OLD_SCHEMA_EPOCH,
    )


def _register_reviewed_release(*args, **kwargs):
    kwargs["operation_lock"] = OPERATION_LOCK
    return rollout.register_reviewed_release(*args, **kwargs)


def _closure_report(account_id: str) -> rollout.LiveCanaryClosureReport:
    phase_by_account = {
        "account-a": rollout.PHASE_ACCOUNT_A_CANARY,
        "account-b": rollout.PHASE_ACCOUNT_B_ROLLOUT,
        "account-c": rollout.PHASE_ACCOUNT_C_ROLLOUT,
        "account-d": rollout.PHASE_ACCOUNT_D_ROLLOUT,
    }
    return rollout.LiveCanaryClosureReport(
        account_id=account_id,
        node_id=rollout.EXPECTED_NODE_IDS[account_id],
        rollout_phase=phase_by_account[account_id],
        release_id=RELEASE_ID,
        image_digest=IMAGE_DIGEST,
        config_sha256=CONFIG_SHA256,
        dependency_lock_sha256=LOCK_SHA256,
        report_sha256=account_id[-1] * 64,
        signature_sha256="8" * 64,
        public_key_sha256=(
            rollout.PINNED_REVIEWER_PUBLIC_KEY_SHA256
        ),
        open_filled_quantity="0.1",
        actual_open_notional_usdt="10",
        close_quantity="0.1",
        close_filled_quantity="0.1",
        non_target_portfolio_sha256="9" * 64,
    )


def _advance_rollout(*args, **kwargs):
    target_phase = kwargs.get("to_phase")
    account_id = rollout.CLOSURE_ACCOUNT_BY_TARGET_PHASE.get(
        target_phase
    )
    if account_id:
        kwargs.setdefault(
            "closure_report_path",
            Path(f"{account_id}.json"),
        )
        kwargs.setdefault(
            "closure_signature_path",
            Path(f"{account_id}.sig"),
        )
        kwargs.setdefault(
            "closure_public_key_path",
            Path("reviewer.pem"),
        )
    kwargs["operation_lock"] = OPERATION_LOCK
    return rollout.advance_rollout(*args, **kwargs)


@pytest.fixture
def rollout_transition_db(migrated_db: str):
    try:
        yield migrated_db
    finally:
        with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM reviewed_release_rollout_events
                WHERE release_id=%s
                """,
                (RELEASE_ID,),
            )
            cur.execute(
                """
                DELETE FROM reviewed_release_rollouts
                WHERE release_id=%s
                """,
                (RELEASE_ID,),
            )


def test_registration_is_atomic_audited_and_idempotent(
    migrated_db: str,
    tmp_path: Path,
) -> None:
    document = _document()
    capacity = rollout.load_capacity_evidence(
        _write_capacity_evidence(tmp_path)
    )
    conn = psycopg2.connect(migrated_db)
    try:
        first = _register_reviewed_release(
            conn,
            document,
            capacity,
            reviewed_by="release-reviewer",
            idempotency_key="register-release-a",
        )
        second = _register_reviewed_release(
            conn,
            document,
            capacity,
            reviewed_by="release-reviewer",
            idempotency_key="register-release-a",
        )
    finally:
        conn.close()

    assert first["phase"] == rollout.PHASE_ACCOUNT_A_CANARY
    assert first["phase_version"] == 1
    assert first["redis_fencing_epoch"] == REDIS_FENCING_EPOCH
    assert first["idempotent"] is False
    assert second["idempotent"] is True

    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT account_id, review_status
            FROM reviewed_release_manifests
            WHERE release_id=%s
            ORDER BY account_id
            """,
            (RELEASE_ID,),
        )
        assert cur.fetchall() == [
            ("account-a", "reviewed"),
            ("account-b", "reviewed"),
            ("account-c", "reviewed"),
            ("account-d", "reviewed"),
        ]
        cur.execute(
            """
            SELECT event_type, from_phase, to_phase, phase_version
            FROM reviewed_release_rollout_events
            WHERE release_id=%s
            """,
            (RELEASE_ID,),
        )
        assert cur.fetchall() == [
            (
                "registered",
                None,
                rollout.PHASE_ACCOUNT_A_CANARY,
                1,
            )
        ]
        cur.execute(
            """
            SELECT event_type
            FROM audit_events
            WHERE aggregate_type='reviewed_release_rollout'
              AND aggregate_id=%s
            """,
            (RELEASE_ID,),
        )
        assert cur.fetchall() == [("reviewed_release_registered",)]
        cur.execute(
            """
            SELECT redis_fencing_epoch::text,
                   domain,
                   status,
                   marker_sha256,
                   capacity_evidence_sha256,
                   initial_redis_run_id,
                   active_volume,
                   activated_by
            FROM redis_fencing_epochs
            WHERE status='active'
            """
        )
        assert cur.fetchone() == (
            REDIS_FENCING_EPOCH,
            "trader-v3",
            "active",
            capacity.marker_sha256,
            capacity.capacity_evidence_sha256,
            capacity.initial_redis_run_id,
            capacity.active_volume,
            "release-reviewer",
        )


def test_registration_rejects_document_hash_drift(
    migrated_db: str,
    tmp_path: Path,
) -> None:
    document = _document()
    capacity = rollout.load_capacity_evidence(
        _write_capacity_evidence(tmp_path)
    )
    conn = psycopg2.connect(migrated_db)
    try:
        _register_reviewed_release(
            conn,
            document,
            capacity,
            reviewed_by="release-reviewer",
            idempotency_key="register-release-a",
        )
        changed = rollout.ReleaseDocument(
            release_id=document.release_id,
            image_digest=document.image_digest,
            config_sha256=document.config_sha256,
            dependency_lock_sha256=document.dependency_lock_sha256,
            schema_epoch=document.schema_epoch,
            manifest_sha256="9" * 64,
            bundle_manifest_sha256=document.bundle_manifest_sha256,
            delivery_mode=document.delivery_mode,
            release_root_path=document.release_root_path,
            release_source_manifest_sha256=(
                document.release_source_manifest_sha256
            ),
            live_adapter_sha256=document.live_adapter_sha256,
            node_ids=document.node_ids,
        )
        with pytest.raises(
            rollout.ReleaseRolloutError,
            match="document hashes conflict",
        ):
            _register_reviewed_release(
                conn,
                changed,
                capacity,
                reviewed_by="release-reviewer",
                idempotency_key="register-release-a",
            )
    finally:
        conn.close()


def test_registration_retires_previous_active_redis_epoch(
    migrated_db: str,
    tmp_path: Path,
) -> None:
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO redis_fencing_epochs (
                redis_fencing_epoch,
                domain,
                status,
                marker_sha256,
                capacity_evidence_sha256,
                initial_redis_run_id,
                active_volume,
                activated_by,
                activated_at
            )
            VALUES (
                %s, 'trader-v3', 'active', %s, %s, %s, %s, %s, now()
            )
            """,
            (
                PREVIOUS_REDIS_FENCING_EPOCH,
                sha256(
                    PREVIOUS_REDIS_FENCING_EPOCH.encode("ascii")
                ).hexdigest(),
                "c" * 64,
                "c" * 40,
                "redis-previous",
                "previous-release",
            ),
        )
    capacity = rollout.load_capacity_evidence(
        _write_capacity_evidence(tmp_path)
    )
    conn = psycopg2.connect(migrated_db)
    try:
        _register_reviewed_release(
            conn,
            _document(),
            capacity,
            reviewed_by="release-reviewer",
            idempotency_key="register-release-a",
        )
    finally:
        conn.close()

    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT redis_fencing_epoch::text, status, retired_at IS NOT NULL
            FROM redis_fencing_epochs
            ORDER BY created_at, redis_fencing_epoch
            """
        )
        rows = {
            epoch: (status, retired)
            for epoch, status, retired in cur.fetchall()
        }
    assert rows == {
        PREVIOUS_REDIS_FENCING_EPOCH: ("retired", True),
        REDIS_FENCING_EPOCH: ("active", False),
    }


def test_cli_register_and_status_use_the_audited_database_path(
    migrated_db: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    document = _document()
    capacity = rollout.load_capacity_evidence(
        _write_capacity_evidence(tmp_path)
    )
    monkeypatch.setattr(
        rollout,
        "load_release_document",
        lambda *_args, **_kwargs: document,
    )
    monkeypatch.setattr(
        rollout,
        "load_capacity_evidence",
        lambda *_args, **_kwargs: capacity,
    )
    monkeypatch.setattr(
        rollout,
        "AccountStallOperationLock",
        lambda **_kwargs: OPERATION_LOCK,
    )
    register_command = [
        "--database-url",
        migrated_db,
        "register",
        "--manifest",
        str(tmp_path / "release-manifest.json"),
        "--bundle-manifest",
        str(tmp_path / "bundle-manifest.json"),
        "--capacity-evidence",
        str(tmp_path / "capacity-evidence.json"),
        "--reviewed-by",
        "release-reviewer",
        "--idempotency-key",
        "cli-register-release",
    ]

    assert rollout.main(register_command) == 0
    registered = json.loads(capsys.readouterr().out)
    assert rollout.main(register_command) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert rollout.main(
        [
            "--database-url",
            migrated_db,
            "status",
            "--release-id",
            registered["release_id"],
        ]
    ) == 0
    status_payload = json.loads(capsys.readouterr().out)

    assert registered["idempotent"] is False
    assert repeated["idempotent"] is True
    assert status_payload["phase"] == rollout.PHASE_ACCOUNT_A_CANARY
    assert status_payload["events"][0]["event_type"] == "registered"


def test_phase_transitions_require_halted_matching_heartbeats(
    rollout_transition_db: str,
    tmp_path: Path,
) -> None:
    migrated_db = rollout_transition_db
    document = _document()
    capacity = rollout.load_capacity_evidence(
        _write_capacity_evidence(tmp_path)
    )
    conn = psycopg2.connect(migrated_db)
    try:
        _register_reviewed_release(
            conn,
            document,
            capacity,
            reviewed_by="release-reviewer",
            idempotency_key="register-release-a",
        )
        with pytest.raises(
            rollout.ReleaseRolloutError,
            match=(
                "account-a requires exactly one fresh "
                "account-b rollout heartbeat"
            ),
        ):
            _advance_rollout(
                conn,
                RELEASE_ID,
                to_phase=rollout.PHASE_ACCOUNT_B_ROLLOUT,
                actor="release-operator",
                reason="account-a canary passed",
                idempotency_key="advance-account-b",
            )
    finally:
        conn.close()

    _seed_heartbeat(
        migrated_db,
        account_id="account-a",
    )
    _seed_approved_old_release(migrated_db, "account-b")
    conn = psycopg2.connect(migrated_db)
    try:
        account_b = _advance_rollout(
            conn,
            RELEASE_ID,
            to_phase=rollout.PHASE_ACCOUNT_B_ROLLOUT,
            actor="release-operator",
            reason="account-a canary passed",
            idempotency_key="advance-account-b",
        )
        repeated = _advance_rollout(
            conn,
            RELEASE_ID,
            to_phase=rollout.PHASE_ACCOUNT_B_ROLLOUT,
            actor="release-operator",
            reason="account-a canary passed",
            idempotency_key="advance-account-b",
        )
        with pytest.raises(
            rollout.ReleaseRolloutError,
            match="account-b must run the new reviewed release",
        ):
            _advance_rollout(
                conn,
                RELEASE_ID,
                to_phase=rollout.PHASE_ACCOUNT_C_ROLLOUT,
                actor="release-operator",
                reason="account-b rollout passed",
                idempotency_key="advance-account-c",
            )
    finally:
        conn.close()

    assert account_b["phase"] == rollout.PHASE_ACCOUNT_B_ROLLOUT
    assert account_b["phase_version"] == 2
    assert repeated["idempotent"] is True

    _seed_heartbeat(
        migrated_db,
        account_id="account-b",
    )
    _seed_approved_old_release(migrated_db, "account-c")
    conn = psycopg2.connect(migrated_db)
    try:
        account_c = _advance_rollout(
            conn,
            RELEASE_ID,
            to_phase=rollout.PHASE_ACCOUNT_C_ROLLOUT,
            actor="release-operator",
            reason="account-b rollout passed",
            idempotency_key="advance-account-c",
        )
        with pytest.raises(
            rollout.ReleaseRolloutError,
            match="account-c must run the new reviewed release",
        ):
            _advance_rollout(
                conn,
                RELEASE_ID,
                to_phase=rollout.PHASE_ACCOUNT_D_ROLLOUT,
                actor="release-operator",
                reason="account-c rollout passed",
                idempotency_key="advance-account-d",
            )
    finally:
        conn.close()

    assert account_c["phase"] == rollout.PHASE_ACCOUNT_C_ROLLOUT
    assert account_c["phase_version"] == 3

    _seed_heartbeat(
        migrated_db,
        account_id="account-c",
    )
    _seed_approved_old_release(migrated_db, "account-d")
    conn = psycopg2.connect(migrated_db)
    try:
        account_d = _advance_rollout(
            conn,
            RELEASE_ID,
            to_phase=rollout.PHASE_ACCOUNT_D_ROLLOUT,
            actor="release-operator",
            reason="account-c rollout passed",
            idempotency_key="advance-account-d",
        )
        with pytest.raises(
            rollout.ReleaseRolloutError,
            match="account-d heartbeat release identity mismatch",
        ):
            _advance_rollout(
                conn,
                RELEASE_ID,
                to_phase=rollout.PHASE_FLEET_COMPLETE,
                actor="release-operator",
                reason="account-d rollout passed",
                idempotency_key="complete-fleet",
            )
    finally:
        conn.close()

    assert account_d["phase"] == rollout.PHASE_ACCOUNT_D_ROLLOUT
    assert account_d["phase_version"] == 4

    _seed_heartbeat(
        migrated_db,
        account_id="account-d",
    )
    conn = psycopg2.connect(migrated_db)
    try:
        complete = _advance_rollout(
            conn,
            RELEASE_ID,
            to_phase=rollout.PHASE_FLEET_COMPLETE,
            actor="release-operator",
            reason="account-d rollout passed",
            idempotency_key="complete-fleet",
        )
        with pytest.raises(
            rollout.ReleaseRolloutError,
            match="invalid reviewed release rollout transition",
        ):
            _advance_rollout(
                conn,
                RELEASE_ID,
                to_phase=rollout.PHASE_ABORTED,
                actor="release-operator",
                reason="late abort",
                idempotency_key="late-abort",
            )
    finally:
        conn.close()

    assert complete["phase"] == rollout.PHASE_FLEET_COMPLETE
    assert complete["phase_version"] == 5
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT from_phase, to_phase, phase_version
            FROM reviewed_release_rollout_events
            WHERE release_id=%s
            ORDER BY phase_version
            """,
            (RELEASE_ID,),
        )
        assert cur.fetchall() == [
            (None, rollout.PHASE_ACCOUNT_A_CANARY, 1),
            (
                rollout.PHASE_ACCOUNT_A_CANARY,
                rollout.PHASE_ACCOUNT_B_ROLLOUT,
                2,
            ),
            (
                rollout.PHASE_ACCOUNT_B_ROLLOUT,
                rollout.PHASE_ACCOUNT_C_ROLLOUT,
                3,
            ),
            (
                rollout.PHASE_ACCOUNT_C_ROLLOUT,
                rollout.PHASE_ACCOUNT_D_ROLLOUT,
                4,
            ),
            (
                rollout.PHASE_ACCOUNT_D_ROLLOUT,
                rollout.PHASE_FLEET_COMPLETE,
                5,
            ),
        ]


def test_rollout_can_abort_once_and_remains_terminal(
    migrated_db: str,
    tmp_path: Path,
) -> None:
    capacity = rollout.load_capacity_evidence(
        _write_capacity_evidence(tmp_path)
    )
    conn = psycopg2.connect(migrated_db)
    try:
        _register_reviewed_release(
            conn,
            _document(),
            capacity,
            reviewed_by="release-reviewer",
            idempotency_key="register-release-a",
        )
        aborted = _advance_rollout(
            conn,
            RELEASE_ID,
            to_phase=rollout.PHASE_ABORTED,
            actor="release-operator",
            reason="canary evidence failed",
            idempotency_key="abort-release-a",
        )
        repeated = _advance_rollout(
            conn,
            RELEASE_ID,
            to_phase=rollout.PHASE_ABORTED,
            actor="release-operator",
            reason="canary evidence failed",
            idempotency_key="abort-release-a",
        )
        with pytest.raises(
            rollout.ReleaseRolloutError,
            match="invalid reviewed release rollout transition",
        ):
            _advance_rollout(
                conn,
                RELEASE_ID,
                to_phase=rollout.PHASE_ACCOUNT_B_ROLLOUT,
                actor="release-operator",
                reason="resume aborted rollout",
                idempotency_key="resume-aborted-release",
            )
    finally:
        conn.close()

    assert aborted["phase"] == rollout.PHASE_ABORTED
    assert aborted["phase_version"] == 2
    assert repeated["idempotent"] is True


def test_phase_advance_rejects_heartbeat_from_another_redis_epoch(
    migrated_db: str,
    tmp_path: Path,
) -> None:
    capacity = rollout.load_capacity_evidence(
        _write_capacity_evidence(tmp_path)
    )
    conn = psycopg2.connect(migrated_db)
    try:
        _register_reviewed_release(
            conn,
            _document(),
            capacity,
            reviewed_by="release-reviewer",
            idempotency_key="register-release-a",
        )
    finally:
        conn.close()
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO redis_fencing_epochs (
                redis_fencing_epoch,
                domain,
                status,
                marker_sha256,
                capacity_evidence_sha256,
                initial_redis_run_id,
                active_volume,
                activated_by,
                activated_at,
                retired_at
            )
            VALUES (
                %s, 'trader-v3', 'retired', %s, %s, %s, %s, %s,
                now(), now()
            )
            """,
            (
                REPLACEMENT_REDIS_FENCING_EPOCH,
                sha256(
                    REPLACEMENT_REDIS_FENCING_EPOCH.encode("ascii")
                ).hexdigest(),
                "d" * 64,
                "b" * 40,
                "redis-replacement",
                "test",
            ),
        )

    _seed_heartbeat(
        migrated_db,
        account_id="account-a",
        redis_fencing_epoch=REPLACEMENT_REDIS_FENCING_EPOCH,
    )
    conn = psycopg2.connect(migrated_db)
    try:
        with pytest.raises(
            rollout.ReleaseRolloutError,
            match="heartbeat Redis fencing epoch mismatch",
        ):
            _advance_rollout(
                conn,
                RELEASE_ID,
                to_phase=rollout.PHASE_ACCOUNT_B_ROLLOUT,
                actor="release-operator",
                reason="account-a canary passed",
                idempotency_key="advance-account-b",
            )
    finally:
        conn.close()


def test_phase_advance_rejects_rollout_after_active_epoch_changes(
    migrated_db: str,
    tmp_path: Path,
) -> None:
    capacity = rollout.load_capacity_evidence(
        _write_capacity_evidence(tmp_path)
    )
    conn = psycopg2.connect(migrated_db)
    try:
        _register_reviewed_release(
            conn,
            _document(),
            capacity,
            reviewed_by="release-reviewer",
            idempotency_key="register-release-a",
        )
    finally:
        conn.close()
    with psycopg2.connect(migrated_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE redis_fencing_epochs
            SET status='retired',
                retired_at=now()
            WHERE redis_fencing_epoch=%s
            """,
            (REDIS_FENCING_EPOCH,),
        )
        cur.execute(
            """
            INSERT INTO redis_fencing_epochs (
                redis_fencing_epoch,
                domain,
                status,
                marker_sha256,
                capacity_evidence_sha256,
                initial_redis_run_id,
                active_volume,
                activated_by,
                activated_at
            )
            VALUES (
                %s, 'trader-v3', 'active', %s, %s, %s, %s, %s, now()
            )
            """,
            (
                REPLACEMENT_REDIS_FENCING_EPOCH,
                sha256(
                    REPLACEMENT_REDIS_FENCING_EPOCH.encode("ascii")
                ).hexdigest(),
                "d" * 64,
                "b" * 40,
                "redis-replacement",
                "test",
            ),
        )

    conn = psycopg2.connect(migrated_db)
    try:
        with pytest.raises(
            rollout.ReleaseRolloutError,
            match="rollout Redis fencing epoch is not active",
        ):
            _advance_rollout(
                conn,
                RELEASE_ID,
                to_phase=rollout.PHASE_ACCOUNT_B_ROLLOUT,
                actor="release-operator",
                reason="account-a canary passed",
                idempotency_key="advance-account-b",
            )
    finally:
        conn.close()
