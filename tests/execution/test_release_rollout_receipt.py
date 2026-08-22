from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
if str(EXECUTION_DOMAIN_ROOT) not in sys.path:
    sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from execution_domain.control_plane import (
    HeartbeatReceipt,
    PeerIdentityReceipt,
    ReleaseGateReceipt,
)


def _peer(*, status: str, fresh: bool, identity_matches: bool):
    return PeerIdentityReceipt(
        node_id="node-b",
        account_id="account-b",
        release_id="release-b",
        image_digest="sha256:" + ("1" * 64),
        config_sha256="2" * 64,
        dependency_lock_sha256="3" * 64,
        schema_epoch="0017_operator_query_projection_reads",
        redis_fencing_epoch="11111111-1111-4111-8111-111111111111",
        freshness_age_seconds=0.5,
        fresh=fresh,
        identity_matches=identity_matches,
        status=status,
    )


def test_rollout_pending_peer_keeps_halted_canary_receipt_non_blocking() -> None:
    receipt = HeartbeatReceipt(
        release_gate=ReleaseGateReceipt(
            status="pass",
            release_id="release-a",
            reviewed_manifest=None,
            rollout_phase="account_a_canary",
            live_open_mode="canary_only",
            phase_version=1,
        ),
        peers=(
            _peer(
                status="rollout_pending",
                fresh=True,
                identity_matches=False,
            ),
        ),
    )

    assert receipt.requires_sticky_halt is False


def test_fleet_identity_drift_is_warning_and_aborted_rollout_is_blocking() -> None:
    drift = HeartbeatReceipt(
        release_gate=ReleaseGateReceipt(
            status="pass",
            release_id="release-a",
            reviewed_manifest=None,
            rollout_phase="fleet_complete",
            live_open_mode="normal",
            phase_version=5,
        ),
        peers=(
            _peer(
                status="identity_drift",
                fresh=True,
                identity_matches=False,
            ),
        ),
    )
    aborted = HeartbeatReceipt(
        release_gate=ReleaseGateReceipt(
            status="aborted",
            release_id="release-a",
            reviewed_manifest=None,
            rollout_phase="aborted",
            live_open_mode="canary_only",
            phase_version=2,
        ),
    )

    assert drift.requires_sticky_halt is False
    assert aborted.requires_sticky_halt is True
