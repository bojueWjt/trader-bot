"""Robot-owned order identity helpers.

Manual exchange activity stays outside the robot order id namespace. Runtime
guards, operator terminal commands, and resume gates use this module as the
shared boundary for read-only user objects.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

ROBOT_CLIENT_ORDER_ID_PREFIX = "B"
ROBOT_CLIENT_ORDER_ID_PATTERN = re.compile(r"^B[0-9a-f]{32}[0-9]{2}$")


def is_robot_client_order_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and ROBOT_CLIENT_ORDER_ID_PATTERN.fullmatch(value.strip()) is not None
    )


def client_order_id_from_row(row: Mapping[str, Any]) -> str:
    for field_name in (
        "client_order_id",
        "clientOrderId",
        "client_algo_id",
        "clientAlgoId",
    ):
        value = row.get(field_name)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def row_is_robot_order(row: Mapping[str, Any]) -> bool:
    return is_robot_client_order_id(client_order_id_from_row(row))


def object_client_order_id(order: Any) -> str:
    for field_name in (
        "client_order_id",
        "clientOrderId",
        "client_algo_id",
        "clientAlgoId",
    ):
        value = getattr(order, field_name, None)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def object_is_robot_order(order: Any) -> bool:
    return is_robot_client_order_id(object_client_order_id(order))


def row_has_robot_owner(row: Mapping[str, Any]) -> bool:
    if row_is_robot_order(row):
        return True
    raw_tags = row.get("tags")
    if isinstance(raw_tags, (list, tuple)):
        for tag in raw_tags:
            text = str(tag)
            if text.startswith("intent_id="):
                return True
    for field_name in (
        "owner_client_order_id",
        "entry_client_order_id",
        "source_client_order_id",
    ):
        if is_robot_client_order_id(str(row.get(field_name) or "")):
            return True
    return False
