from __future__ import annotations

from settings.resolver import canonical_scope_key, resolve_effective_settings


def test_resolver_applies_global_account_instrument_precedence_with_metadata() -> None:
    layers = {
        ("global", "global"): {
            "general": {"order_manager_enabled": True},
            "entry": {"max_slippage_bps": 50},
        },
        ("account", "acct-main"): {
            "entry": {"max_slippage_bps": 10},
        },
        ("instrument", "BTCUSDT"): {
            "entry": {"max_slippage_bps": 5},
        },
    }

    resolved = resolve_effective_settings(
        settings_by_scope=layers,
        account_id=" acct-main ",
        instrument_id="BTCUSDT-PERP.BINANCE",
    )

    assert resolved["entry"]["max_slippage_bps"] == {
        "value": 5,
        "source_scope": "instrument",
        "inherited": False,
        "validation": {"valid": True, "errors": []},
        "apply_mode": "hot_reload",
    }
    assert resolved["general"]["order_manager_enabled"]["value"] is True
    assert resolved["general"]["order_manager_enabled"]["source_scope"] == "global"
    assert resolved["general"]["order_manager_enabled"]["inherited"] is True


def test_delete_restore_inheritance_by_removing_higher_layer_override() -> None:
    global_layer = {("global", "global"): {"entry": {"max_slippage_bps": 50}}}
    account_layer = {("account", "acct-main"): {"entry": {"max_slippage_bps": 10}}}
    instrument_layer = {("instrument", "BTCUSDT"): {"entry": {"max_slippage_bps": 5}}}

    with_instrument = resolve_effective_settings(
        settings_by_scope={**global_layer, **account_layer, **instrument_layer},
        account_id="acct-main",
        instrument_id="BTCUSDT",
    )
    after_instrument_delete = resolve_effective_settings(
        settings_by_scope={**global_layer, **account_layer, ("instrument", "BTCUSDT"): {"entry": {}}},
        account_id="acct-main",
        instrument_id="BTCUSDT",
    )
    after_account_delete = resolve_effective_settings(
        settings_by_scope={**global_layer, ("account", "acct-main"): {"entry": {}}},
        account_id="acct-main",
        instrument_id="BTCUSDT",
    )

    assert with_instrument["entry"]["max_slippage_bps"]["value"] == 5
    assert after_instrument_delete["entry"]["max_slippage_bps"]["value"] == 10
    assert after_instrument_delete["entry"]["max_slippage_bps"]["source_scope"] == "account"
    assert after_instrument_delete["entry"]["max_slippage_bps"]["inherited"] is True
    assert after_account_delete["entry"]["max_slippage_bps"]["value"] == 50
    assert after_account_delete["entry"]["max_slippage_bps"]["source_scope"] == "global"


def test_scope_keys_are_canonicalized_for_accounts_and_instruments() -> None:
    assert canonical_scope_key("global", None) == "global"
    assert canonical_scope_key("account", " acct-main ") == "acct-main"
    assert canonical_scope_key("instrument", "BTCUSDT-PERP.BINANCE") == "BTCUSDT"


def test_resolved_field_carries_validation_errors_for_invalid_persisted_value() -> None:
    resolved = resolve_effective_settings(
        settings_by_scope={("global", "global"): {"entry": {"max_slippage_bps": -1}}},
    )

    field = resolved["entry"]["max_slippage_bps"]
    assert field["value"] == -1
    assert field["source_scope"] == "global"
    assert field["validation"]["valid"] is False
    assert field["validation"]["errors"][0]["path"] == "entry.max_slippage_bps"
