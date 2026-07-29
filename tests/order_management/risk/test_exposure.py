from __future__ import annotations

from decimal import Decimal

from risk_config import RiskConfig
from exposure import ProposedExposure, check_exposure, exposure_from_positions


def test_exposure_uses_canonical_instrument_keys_for_existing_and_proposed() -> None:
    positions = [
        {
            "account_id": "acct-om3-a",
            "instrument_id": "BTCUSDT-PERP.BINANCE",
            "quantity": Decimal("1"),
            "avg_entry_price": Decimal("500"),
            "status": "open",
        }
    ]
    snapshot = exposure_from_positions(positions, account_id="acct-om3-a")
    proposed = ProposedExposure(
        account_id="acct-om3-a",
        instrument_id="BTCUSDT",
        notional=Decimal("600"),
        risk_amount=Decimal("30"),
    )

    result = check_exposure(snapshot, proposed, RiskConfig(max_instrument_exposure=Decimal("1000")))

    assert result.status == "rejected"
    assert result.projected_instrument_exposure == Decimal("1100")
    assert result.headroom["instrument"] == Decimal("500")


def test_exposure_isolated_by_account_when_checking_account_caps() -> None:
    positions = [
        {"account_id": "acct-om3-a", "instrument_id": "BTCUSDT", "quantity": Decimal("1"), "avg_entry_price": Decimal("900"), "status": "open"},
        {"account_id": "acct-om3-b", "instrument_id": "BTCUSDT", "quantity": Decimal("1"), "avg_entry_price": Decimal("900"), "status": "open"},
    ]

    snapshot = exposure_from_positions(positions, account_id="acct-om3-a")
    proposed = ProposedExposure("acct-om3-a", "BTCUSDT", Decimal("50"), Decimal("5"))
    result = check_exposure(snapshot, proposed, RiskConfig(max_instrument_exposure=Decimal("1000")))

    assert result.status == "approved"
    assert result.projected_instrument_exposure == Decimal("950")


def test_correlated_exposure_includes_group_members_and_proposed_notional() -> None:
    positions = [
        {"account_id": "acct-om3-a", "instrument_id": "ETHUSDT-PERP.BINANCE", "quantity": Decimal("1"), "avg_entry_price": Decimal("1000"), "status": "open"},
    ]
    snapshot = exposure_from_positions(positions, account_id="acct-om3-a")
    proposed = ProposedExposure("acct-om3-a", "BTCUSDT", Decimal("600"), Decimal("30"))

    result = check_exposure(
        snapshot,
        proposed,
        RiskConfig(max_correlated_exposure=Decimal("1500")),
        correlated_groups={"majors": ("BTCUSDT", "ETHUSDT")},
    )

    assert result.status == "rejected"
    assert result.projected_correlated_exposure == Decimal("1600")
    assert result.correlated_group == "majors"

