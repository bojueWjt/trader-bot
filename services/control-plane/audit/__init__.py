from __future__ import annotations

# Two different modules are addressed as top-level ``audit`` inside the
# control-plane process: this package (settings audit) and
# ``security/audit.py`` (dangerous-operation audit), which sys.path
# resolution can never satisfy at once — whichever imports first wins
# and the other consumer breaks (`/v1/commands` 500ed on
# ``from audit import dangerous_operation_payload``). Re-export the
# security symbols here so the package satisfies both consumers no
# matter the import order.
from security.audit import (  # noqa: F401
    dangerous_operation_payload,
    record_audit_event,
)

from .settings_audit import record_settings_audit, request_id_uuid

__all__ = [
    "dangerous_operation_payload",
    "record_audit_event",
    "record_settings_audit",
    "request_id_uuid",
]
