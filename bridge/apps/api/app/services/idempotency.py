from __future__ import annotations

from dataclasses import dataclass
from threading import Lock


@dataclass(frozen=True)
class ReservationResult:
    reserved: bool
    signal_id: str
    operation_type: str
    reason: str = ""


class AtomicReservationStore:
    def __init__(self) -> None:
        self._reserved_operations: set[tuple[str, str]] = set()
        self._lock = Lock()

    def reserve(self, signal_id: str, operation_type: str) -> ReservationResult:
        operation_key = (signal_id, operation_type)
        with self._lock:
            if operation_key in self._reserved_operations:
                return ReservationResult(
                    reserved=False,
                    signal_id=signal_id,
                    operation_type=operation_type,
                    reason="duplicate_operation",
                )
            self._reserved_operations.add(operation_key)
            return ReservationResult(
                reserved=True,
                signal_id=signal_id,
                operation_type=operation_type,
            )

    def count(self, signal_id: str, operation_type: str) -> int:
        operation_key = (signal_id, operation_type)
        with self._lock:
            if operation_key in self._reserved_operations:
                return 1
            return 0
