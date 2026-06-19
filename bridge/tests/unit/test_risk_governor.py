from datetime import datetime, timedelta, timezone

from app.services.risk_governor import (
    JsonRiskStateStore,
    PairLockStore,
    RiskGovernor,
    default_risk_policy,
)


def make_signal(**overrides):
    signal = {
        "signal_id": "sig-1",
        "pair": "BTC/USDT:USDT",
        "side": "long",
        "entry_price": 71000,
        "stop_loss": 70400,
        "take_profits": [72000],
        "leverage": 3,
        "notional": 1000,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "liquidation_buffer_pct": 5.0,
        "run_mode": "dry_run",
    }
    signal.update(overrides)
    return signal


def make_account(**overrides):
    account = {
        "equity": 10000,
        "open_risk_pct": 1.0,
        "daily_realized_loss_pct": 0.0,
        "symbol_exposure_pct": {"BTC/USDT:USDT": 3.0},
        "correlated_group_exposure_pct": {"majors": 10.0},
        "pair_groups": {"BTC/USDT:USDT": "majors"},
    }
    account.update(overrides)
    return account


def test_default_policy_matches_shared_contract():
    policy = default_risk_policy()

    assert policy.max_single_trade_risk_pct == 1.0
    assert policy.max_total_open_risk_pct == 5.0
    assert policy.max_daily_realized_loss_pct == 3.0
    assert policy.default_leverage == 3
    assert policy.max_leverage == 5
    assert policy.allow_live_without_stop_loss is False


def test_long_price_geometry_passes():
    result = RiskGovernor().precheck(make_signal(), make_account())

    assert result["decision"] == "approved"
    assert "price_geometry_invalid" not in result["reason_codes"]


def test_short_price_geometry_passes():
    signal = make_signal(side="short", entry_price=71000, stop_loss=72000, take_profits=[70400])

    result = RiskGovernor().precheck(signal, make_account())

    assert result["decision"] == "approved"


def test_gs_005_short_geometry_blocks():
    signal = make_signal(side="short", entry_price=71000, stop_loss=70400, take_profits=[72000])

    result = RiskGovernor().precheck(signal, make_account())

    assert result["decision"] == "blocked"
    assert "price_geometry_invalid" in result["reason_codes"]


def test_single_trade_risk_blocks():
    signal = make_signal(entry_price=71000, stop_loss=65000, notional=3000)

    result = RiskGovernor().precheck(signal, make_account())

    assert result["decision"] == "blocked"
    assert "single_trade_risk_exceeded" in result["reason_codes"]


def test_total_open_risk_blocks():
    result = RiskGovernor().precheck(make_signal(), make_account(open_risk_pct=4.95))

    assert result["decision"] == "blocked"
    assert "total_open_risk_exceeded" in result["reason_codes"]


def test_symbol_and_group_exposure_include_new_trade():
    result = RiskGovernor().precheck(
        make_signal(notional=1000),
        make_account(
            symbol_exposure_pct={"BTC/USDT:USDT": 14.5},
            correlated_group_exposure_pct={"majors": 34.5},
        ),
    )

    assert "symbol_exposure_exceeded" in result["reason_codes"]
    assert "correlated_group_exposure_exceeded" in result["reason_codes"]


def test_daily_loss_moves_to_blocked_new_entries():
    result = RiskGovernor().precheck(
        make_signal(),
        make_account(daily_realized_loss_pct=3.1),
    )

    assert result["risk_state"] == "blocked_new_entries"
    assert "daily_loss_exceeded" in result["reason_codes"]


def test_blocked_new_entries_allows_reducing_actions():
    governor = RiskGovernor()
    governor.set_risk_state("blocked_new_entries")

    close_result = governor.precheck(make_signal(action="close"), make_account())
    entry_result = governor.precheck(make_signal(action="entry"), make_account())

    assert close_result["decision"] == "approved"
    assert entry_result["decision"] == "blocked"
    assert "risk_state_blocks_new_entries" in entry_result["reason_codes"]


def test_live_without_stop_loss_blocks():
    signal = make_signal(run_mode="live", stop_loss=False)

    result = RiskGovernor().precheck(signal, make_account())

    assert result["decision"] == "blocked"
    assert "live_stop_loss_required" in result["reason_codes"]


def test_live_without_take_profit_requires_manual_approval():
    signal = make_signal(run_mode="live", take_profits=[])

    result = RiskGovernor().precheck(signal, make_account())

    assert result["decision"] == "blocked"
    assert "live_take_profit_requires_manual_approval" in result["reason_codes"]


def test_pair_lock_blocks_and_expires():
    store = PairLockStore()
    store.create(
        pair="BTC/USDT:USDT",
        reason="news event",
        actor_id="risk-admin",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    governor = RiskGovernor(pair_lock_store=store)

    locked_result = governor.precheck(make_signal(), make_account())
    expired_result = governor.precheck(make_signal(pair="ETH/USDT:USDT"), make_account())

    assert locked_result["decision"] == "blocked"
    assert "pair_locked" in locked_result["reason_codes"]
    assert expired_result["decision"] == "approved"


def test_pair_lock_accepts_naive_expires_at_as_utc():
    store = PairLockStore()
    store.create(
        pair="BTC/USDT:USDT",
        reason="news event",
        actor_id="risk-admin",
        expires_at="2099-06-01T00:00:00",
    )

    assert len(store.active_for_pair("BTC/USDT:USDT")) == 1


def test_pair_lock_list_does_not_duplicate_same_pair_locks():
    store = PairLockStore()
    for reason in ["event one", "event two"]:
        store.create(
            pair="BTC/USDT:USDT",
            reason=reason,
            actor_id="risk-admin",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

    assert len(store.list_active()) == 2


def test_kill_switch_blocks_new_entries():
    governor = RiskGovernor()
    governor.enable_kill_switch(actor_id="risk-admin", reason="manual emergency stop")

    result = governor.precheck(make_signal(), make_account())

    assert result["risk_state"] == "kill_switch_enabled"
    assert result["decision"] == "blocked"
    assert "kill_switch_enabled" in result["reason_codes"]


def test_risk_state_and_pair_locks_survive_restart(tmp_path):
    state_path = tmp_path / "risk-state.json"
    store = JsonRiskStateStore(state_path)
    pair_locks = PairLockStore(state_store=store)
    governor = RiskGovernor(pair_lock_store=pair_locks, state_store=store)
    pair_locks.create(
        pair="BTC/USDT:USDT",
        reason="event risk",
        actor_id="risk-admin",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    governor.enable_kill_switch(actor_id="risk-admin", reason="manual emergency stop")

    restarted_store = JsonRiskStateStore(state_path)
    restarted_locks = PairLockStore(state_store=restarted_store)
    restarted = RiskGovernor(pair_lock_store=restarted_locks, state_store=restarted_store)

    assert restarted.get_risk_state() == "kill_switch_enabled"
    assert restarted_locks.active_for_pair("BTC/USDT:USDT")
