from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = REPO_ROOT / "services" / "nautilus-node"
EXECUTION_DOMAIN_ROOT = REPO_ROOT / "packages" / "execution-domain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(EXECUTION_DOMAIN_ROOT))

from runtime.live_canary_execution import (  # noqa: E402
    JsonLiveCanaryExecutionStore,
    LiveCanaryClaimResult,
    LiveCanaryExecutionIdentity,
    LiveCanaryExecutionState,
    LiveCanaryFill,
    LiveCanaryMark,
    LiveCanaryRegisterResult,
    deterministic_canary_close_client_order_id,
    is_live_canary_account,
    live_canary_permit_required,
)


def test_rollout_accounts_do_not_require_live_canary_permits_by_default() -> None:
    for account_id in (
        "account-a",
        "account-b",
        "account-c",
        "account-d",
    ):
        assert is_live_canary_account(account_id) is True
        assert live_canary_permit_required(
            account_id,
            "account_a_canary",
        ) is False
        assert live_canary_permit_required(
            account_id,
            "account_d_rollout",
        ) is False


def test_fleet_complete_allows_regular_live_entries() -> None:
    for account_id in (
        "account-a",
        "account-b",
        "account-c",
        "account-d",
    ):
        assert live_canary_permit_required(
            account_id,
            "fleet_complete",
        ) is False


def test_unknown_accounts_do_not_enter_live_canary_runtime() -> None:
    for account_id in ("", "account-e", None):
        assert is_live_canary_account(account_id) is False
        assert live_canary_permit_required(account_id) is False


def test_durable_inbox_binds_one_permit_to_one_intent(tmp_path: Path) -> None:
    store = JsonLiveCanaryExecutionStore(tmp_path / "live-canary.json")
    permit_id = str(uuid4())
    first = _identity(permit_id=permit_id)
    second = _identity(permit_id=permit_id)

    assert store.register_received(first) is LiveCanaryRegisterResult.REGISTERED
    assert store.register_received(first) is LiveCanaryRegisterResult.REPLAY
    assert (
        store.register_received(second)
        is LiveCanaryRegisterResult.PERMIT_CONFLICT
    )

    record = store.get(first)
    assert record
    assert record.state is LiveCanaryExecutionState.RECEIVED
    assert record.intent_id == first.intent_id


def test_durable_inbox_allows_new_permit_for_same_release(
    tmp_path: Path,
) -> None:
    store = JsonLiveCanaryExecutionStore(tmp_path / "live-canary.json")
    first = _identity()
    second = _identity()

    assert first.release_id == second.release_id
    assert first.permit_id != second.permit_id
    assert (
        store.register_received(first)
        is LiveCanaryRegisterResult.REGISTERED
    )
    assert (
        store.register_received(second)
        is LiveCanaryRegisterResult.REGISTERED
    )
    assert store.get(first)
    assert store.get(second)


def test_claim_replay_requires_recovery_and_preserves_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "live-canary.json"
    first = JsonLiveCanaryExecutionStore(path)
    identity = _identity()

    assert first.claim(identity) is LiveCanaryClaimResult.ACQUIRED

    restarted = JsonLiveCanaryExecutionStore(path)
    assert (
        restarted.claim(identity)
        is LiveCanaryClaimResult.RECOVERY_REQUIRED
    )
    record = restarted.get(identity)
    assert record
    assert record.state is LiveCanaryExecutionState.CLAIMED

    restarted.mark_dispatched(identity)
    restarted.mark_exchange_confirmed(identity)

    final_record = JsonLiveCanaryExecutionStore(path).get(identity)
    assert final_record
    assert final_record.state is LiveCanaryExecutionState.EXCHANGE_CONFIRMED


def test_concurrent_claims_for_same_permit_have_one_winner(
    tmp_path: Path,
) -> None:
    path = tmp_path / "live-canary.json"
    permit_id = str(uuid4())
    first = _identity(permit_id=permit_id)
    second = _identity(permit_id=permit_id)

    def claim(identity: LiveCanaryExecutionIdentity) -> LiveCanaryClaimResult:
        store = JsonLiveCanaryExecutionStore(path)
        return store.claim(identity)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, (first, second)))

    assert results.count(LiveCanaryClaimResult.ACQUIRED) == 1
    assert results.count(LiveCanaryClaimResult.PERMIT_CONFLICT) == 1


def test_durable_permit_cannot_be_rebound_across_accounts(
    tmp_path: Path,
) -> None:
    store = JsonLiveCanaryExecutionStore(tmp_path / "live-canary.json")
    permit_id = str(uuid4())
    account_b = _identity(
        permit_id=permit_id,
        account_id="account-b",
        node_id="node-b",
    )
    account_c = _identity(
        permit_id=permit_id,
        account_id="account-c",
        node_id="node-c",
    )

    assert (
        store.register_received(account_b)
        is LiveCanaryRegisterResult.REGISTERED
    )
    assert (
        store.register_received(account_c)
        is LiveCanaryRegisterResult.PERMIT_CONFLICT
    )


def test_fill_accounting_deduplicates_and_tracks_mark_to_market_loss(
    tmp_path: Path,
) -> None:
    store = JsonLiveCanaryExecutionStore(tmp_path / "live-canary.json")
    identity = _identity()
    assert store.claim(identity) is LiveCanaryClaimResult.ACQUIRED
    fill = _fill(identity, fill_id="trade-1", mark_price="99")

    assert store.record_fill(fill) is False
    assert store.record_fill(fill) is False

    record = store.get(identity)
    assert record
    assert record.state is LiveCanaryExecutionState.EXCHANGE_CONFIRMED
    assert record.net_quantity == "0.1"
    assert record.fees_usdt == "0.01"
    assert record.current_loss_usdt == "0.11"
    assert record.peak_loss_usdt == "0.11"
    assert record.processed_fill_ids == ("trade-1",)


def test_loss_limit_breach_persists_exact_reduce_only_close_decision(
    tmp_path: Path,
) -> None:
    store = JsonLiveCanaryExecutionStore(tmp_path / "live-canary.json")
    identity = _identity()
    assert store.claim(identity) is LiveCanaryClaimResult.ACQUIRED

    decision = store.record_fill(
        _fill(identity, fill_id="trade-1", mark_price="80")
    )

    assert decision
    assert decision.close_side == "SELL"
    assert decision.close_quantity == "0.1"
    assert decision.close_client_order_id == (
        deterministic_canary_close_client_order_id(identity.intent_id)
    )
    assert Decimal(decision.peak_loss_usdt) >= Decimal("1.5")
    assert store.pending_close_decisions() == (decision,)

    restarted = JsonLiveCanaryExecutionStore(
        tmp_path / "live-canary.json"
    )
    assert restarted.pending_close_decisions() == (decision,)


def test_mark_to_market_breach_without_new_fill_persists_exact_close(
    tmp_path: Path,
) -> None:
    store = JsonLiveCanaryExecutionStore(tmp_path / "live-canary.json")
    identity = _identity()
    assert store.claim(identity) is LiveCanaryClaimResult.ACQUIRED
    assert store.record_fill(
        _fill(identity, fill_id="entry", mark_price="99")
    ) is False

    decision = store.record_mark(
        LiveCanaryMark(
            client_order_id=identity.client_order_id,
            instrument_id="BTCUSDT-PERP.BINANCE",
            mark_price_usdt="80",
            observed_at="2026-08-08T12:00:01+00:00",
            evaluated_at="2026-08-08T12:00:01+00:00",
        )
    )

    assert decision
    assert decision.close_side == "SELL"
    assert decision.close_quantity == "0.1"
    assert "cumulative loss limit reached" in decision.halt_reason
    record = store.get(identity)
    assert record
    assert record.state is LiveCanaryExecutionState.LOSS_LIMIT_HALTED
    assert record.last_mark_price_usdt == "80"


def test_reduce_only_close_fill_completes_durable_loss_guard(
    tmp_path: Path,
) -> None:
    store = JsonLiveCanaryExecutionStore(tmp_path / "live-canary.json")
    identity = _identity()
    assert store.claim(identity) is LiveCanaryClaimResult.ACQUIRED
    decision = store.record_fill(
        _fill(identity, fill_id="entry", mark_price="80")
    )
    assert decision
    store.mark_close_dispatched(identity)

    close_fill = LiveCanaryFill(
        fill_id="close",
        client_order_id=decision.close_client_order_id,
        instrument_id=decision.instrument_id,
        side="SELL",
        quantity=decision.close_quantity,
        price_usdt="79",
        fee_usdt="0.01",
        mark_price_usdt="79",
        reduce_only=True,
        occurred_at="2026-08-08T12:00:01+00:00",
    )
    assert store.record_fill(close_fill) is False

    record = store.get(identity)
    assert record
    assert record.state is LiveCanaryExecutionState.CLOSED
    assert record.net_quantity == "0.0"
    assert Decimal(record.close_required_quantity) == Decimal("0")
    assert record.realized_pnl_usdt == "-2.1"
    assert record.fees_usdt == "0.02"
    assert store.pending_close_decisions() == ()


def test_incomplete_fill_accounting_halts_and_closes_fail_closed(
    tmp_path: Path,
) -> None:
    store = JsonLiveCanaryExecutionStore(tmp_path / "live-canary.json")
    identity = _identity()
    assert store.claim(identity) is LiveCanaryClaimResult.ACQUIRED

    decision = store.record_fill(
        LiveCanaryFill(
            fill_id="trade-1",
            client_order_id=identity.client_order_id,
            instrument_id="BTCUSDT-PERP.BINANCE",
            side="BUY",
            quantity="0.1",
            price_usdt="100",
            fee_usdt="0",
            mark_price_usdt="0",
            reduce_only=False,
            occurred_at="2026-08-08T12:00:00+00:00",
            accounting_error="fill fee is unavailable",
        )
    )

    assert decision
    assert decision.close_quantity == "0.1"
    assert "accounting incomplete" in decision.halt_reason


def _identity(
    *,
    permit_id: str | None = None,
    account_id: str = "account-a",
    node_id: str = "node-a",
) -> LiveCanaryExecutionIdentity:
    intent_id = str(uuid4())
    return LiveCanaryExecutionIdentity(
        permit_id=permit_id or str(uuid4()),
        release_id="release-a",
        intent_id=intent_id,
        client_order_id=f"B{intent_id.replace('-', '')}01",
        account_id=account_id,
        node_id=node_id,
        symbol="BTCUSDT",
        max_notional_usdt="12",
        max_cumulative_loss_usdt="1.49",
        authorized_limit_price_usdt="100",
        expires_at="2026-08-09T00:00:00+00:00",
        portfolio_baseline_sha256="4" * 64,
    )


def _fill(
    identity: LiveCanaryExecutionIdentity,
    *,
    fill_id: str,
    mark_price: str,
) -> LiveCanaryFill:
    return LiveCanaryFill(
        fill_id=fill_id,
        client_order_id=identity.client_order_id,
        instrument_id="BTCUSDT-PERP.BINANCE",
        side="BUY",
        quantity="0.1",
        price_usdt="100",
        fee_usdt="0.01",
        mark_price_usdt=mark_price,
        reduce_only=False,
        occurred_at="2026-08-08T12:00:00+00:00",
    )
