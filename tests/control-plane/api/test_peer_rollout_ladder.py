"""Peer status ladder logic for the four-account reviewed rollout.

Pure-function coverage of _peer_awaits_rollout_step and its two callers:
no database cluster required. The ladder rule: during an active reviewed
rollout a HALTED reporter tolerates ("rollout_pending") every peer whose
ladder step is the current phase or later; a LIVE reporter tolerates
nothing; outside an active phase (fleet_complete, aborted) nothing is
tolerated.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
READ_API = REPO_ROOT / "services" / "control-plane" / "api" / "read_api.py"

_spec = importlib.util.spec_from_file_location(
    "peer_ladder_read_api", READ_API
)
read_api = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("peer_ladder_read_api", read_api)
_spec.loader.exec_module(read_api)

RELEASE_IDENTITY = ("R1", "img", "cfg", "dep", "sch")


def _missing_status(**overrides) -> str:
    parameters = {
        "account_id": "account-a",
        "trading_state": "HALTED",
        "peer_account_id": "account-b",
        "release_identity": RELEASE_IDENTITY,
        "rollout_phase": "account_a_canary",
    }
    parameters.update(overrides)
    return read_api._missing_peer_status(**parameters)


def test_halted_canary_tolerates_every_later_missing_peer() -> None:
    for peer in ("account-b", "account-c", "account-d"):
        assert _missing_status(peer_account_id=peer) == "rollout_pending"


def test_mid_ladder_phase_tolerates_current_and_later_steps_only() -> None:
    expectations = {
        "account-a": "missing",
        "account-b": "missing",
        "account-c": "rollout_pending",
        "account-d": "rollout_pending",
    }
    for peer, expected in expectations.items():
        assert (
            _missing_status(
                account_id="account-b",
                peer_account_id=peer,
                rollout_phase="account_c_rollout",
            )
            == expected
        )


def test_live_reporter_never_tolerates_missing_peers() -> None:
    assert _missing_status(trading_state="LIVE") == "missing"


def test_inactive_phases_tolerate_nothing() -> None:
    for phase in ("fleet_complete", "aborted", None):
        assert _missing_status(rollout_phase=phase) == "missing"


def test_missing_release_identity_tolerates_nothing() -> None:
    assert (
        _missing_status(release_identity=(None, None, None, None, None))
        == "missing"
    )


def test_stale_peer_awaiting_its_step_reports_rollout_pending() -> None:
    status = read_api._peer_release_status(
        account_id="account-a",
        trading_state="HALTED",
        peer_account_id="account-b",
        peer_trading_state="",
        release_identity=RELEASE_IDENTITY,
        peer_identity=(None, None, None, None, None),
        fresh=False,
        identity_matches=False,
        rollout_phase="account_a_canary",
        active_rollout=None,
    )
    assert status == "rollout_pending"


def test_old_release_reporter_tolerates_fresh_halted_rollout_peer() -> None:
    status = read_api._peer_release_status(
        account_id="account-b",
        trading_state="LIVE",
        peer_account_id="account-a",
        peer_trading_state="HALTED",
        release_identity=("OLD", "i", "c", "d", "s"),
        peer_identity=RELEASE_IDENTITY,
        fresh=True,
        identity_matches=False,
        rollout_phase=None,
        active_rollout={
            "phase": "account_a_canary",
            "release_id": "R1",
        },
    )
    assert status == "rollout_pending"


def test_consistent_short_circuits_before_ladder_tolerance() -> None:
    status = read_api._peer_release_status(
        account_id="account-a",
        trading_state="HALTED",
        peer_account_id="account-b",
        peer_trading_state="HALTED",
        release_identity=RELEASE_IDENTITY,
        peer_identity=RELEASE_IDENTITY,
        fresh=True,
        identity_matches=True,
        rollout_phase="account_b_rollout",
        active_rollout=None,
    )
    assert status == "consistent"


def test_genuinely_stale_peer_after_fleet_complete_reports_stale() -> None:
    status = read_api._peer_release_status(
        account_id="account-a",
        trading_state="LIVE",
        peer_account_id="account-b",
        peer_trading_state="LIVE",
        release_identity=RELEASE_IDENTITY,
        peer_identity=RELEASE_IDENTITY,
        fresh=False,
        identity_matches=True,
        rollout_phase="fleet_complete",
        active_rollout=None,
    )
    assert status == "stale"
