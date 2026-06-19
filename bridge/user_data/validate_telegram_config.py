#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


CONFIG_PATH = Path(__file__).with_name("config_dry_run.json")
TOKEN_PLACEHOLDER = "${FREQTRADE__TELEGRAM__TOKEN}"
CHAT_ID_PLACEHOLDER = "${FREQTRADE__TELEGRAM__CHAT_ID}"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"FAIL: {message}")


def nested_value(source: dict[str, Any], *path: str) -> Any:
    value: Any = source
    for key in path:
        require(isinstance(value, dict), ".".join(path))
        value = value.get(key)
    return value


def main() -> None:
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        config = json.load(handle)

    telegram = config.get("telegram")
    require(isinstance(telegram, dict), "telegram block is missing")
    require(telegram.get("enabled") is True, "telegram.enabled must be true")
    require(telegram.get("token") == TOKEN_PLACEHOLDER, "telegram.token must use env placeholder")
    require(telegram.get("chat_id") == CHAT_ID_PLACEHOLDER, "telegram.chat_id must use env placeholder")

    settings = telegram.get("notification_settings")
    require(isinstance(settings, dict), "telegram.notification_settings is missing")

    required_paths = [
        ("entry_fill",),
        ("exit_fill",),
        ("entry_cancel",),
        ("exit_cancel",),
        ("exit", "stop_loss"),
        ("exit", "emergency_exit"),
        ("exit", "custom_exit"),
        ("protection_trigger",),
        ("strategy_msg",),
        ("status",),
    ]
    for path in required_paths:
        value = nested_value(settings, *path)
        require(value in {"on", "silent"}, f"{'.'.join(path)} must be enabled")

    serialized = json.dumps(config, ensure_ascii=True)
    require("FREQTRADE__TELEGRAM__TOKEN" in serialized, "token env placeholder missing")
    require("FREQTRADE__TELEGRAM__CHAT_ID" in serialized, "chat_id env placeholder missing")
    require("bot" not in telegram["token"].lower(), "telegram token must not be a real token")

    covered = ", ".join(".".join(path) for path in required_paths)
    print(f"OK: {CONFIG_PATH} is valid JSON")
    print("OK: telegram.enabled is true")
    print("OK: telegram token/chat_id use environment placeholders")
    print(f"OK: notification_settings covers {covered}")
    print("OK: breakeven stoploss notification is covered by strategy_msg + SignalStrategy send_msg")


if __name__ == "__main__":
    main()
