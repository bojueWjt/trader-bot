from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db.models_signal import SignalLifecycleEvent, SignalTransitionResult
from app.db.repositories_signal import SignalRepository
from app.services.idempotency import AtomicReservationStore, ReservationResult
from app.services.signal_parser import ParsedSignal, SignalStatus


ALLOWED_TRANSITIONS: dict[SignalStatus, set[SignalStatus]] = {
    SignalStatus.RAW: {SignalStatus.PARSED, SignalStatus.IGNORED},
    SignalStatus.PARSED: {
        SignalStatus.NEEDS_REVIEW,
        SignalStatus.APPROVED,
        SignalStatus.REJECTED,
        SignalStatus.EXPIRED,
        SignalStatus.BLOCKED_BY_RISK,
    },
    SignalStatus.NEEDS_REVIEW: {
        SignalStatus.APPROVED,
        SignalStatus.REJECTED,
        SignalStatus.EXPIRED,
        SignalStatus.BLOCKED_BY_RISK,
    },
    SignalStatus.APPROVED: {
        SignalStatus.RESERVED,
        SignalStatus.EXPIRED,
        SignalStatus.BLOCKED_BY_RISK,
    },
    SignalStatus.RESERVED: {SignalStatus.SENT_TO_FREQTRADE, SignalStatus.FAILED},
    SignalStatus.SENT_TO_FREQTRADE: {
        SignalStatus.ENTERED,
        SignalStatus.FAILED,
        SignalStatus.EXPIRED,
    },
    SignalStatus.ENTERED: {
        SignalStatus.PARTIALLY_EXITED,
        SignalStatus.EXITED,
        SignalStatus.FAILED,
    },
    SignalStatus.PARTIALLY_EXITED: {
        SignalStatus.PARTIALLY_EXITED,
        SignalStatus.EXITED,
    },
    SignalStatus.EXITED: set(),
    SignalStatus.REJECTED: set(),
    SignalStatus.EXPIRED: set(),
    SignalStatus.FAILED: set(),
    SignalStatus.IGNORED: set(),
    SignalStatus.BLOCKED_BY_RISK: set(),
}


class SignalStore:
    def __init__(self, repository: SignalRepository | None = None) -> None:
        if repository:
            self.repository = repository
        else:
            self.repository = SignalRepository()
        self.reservations = AtomicReservationStore()

    def upsert_signal(self, signal: ParsedSignal) -> ParsedSignal:
        return self.repository.upsert(signal)

    def get_signal(self, signal_id: str) -> ParsedSignal:
        signal = self.repository.get(signal_id)
        if not signal:
            raise KeyError(signal_id)
        return signal

    def count(self) -> int:
        return self.repository.count()

    def list_approved(
        self,
        since_minutes: int = 240,
        current_time: datetime | None = None,
    ) -> list[ParsedSignal]:
        approved = self.repository.list_by_status(SignalStatus.APPROVED)
        if current_time is None:
            current_time = datetime.now(timezone.utc)
        cutoff = current_time - timedelta(minutes=since_minutes)
        return [
            signal
            for signal in approved
            if _signal_received_at(signal) >= cutoff
        ]

    def reserve_signal(
        self,
        signal_id: str,
        operation_type: str = "entry",
    ) -> ReservationResult:
        reservation = self.reservations.reserve(signal_id, operation_type)
        if not reservation.reserved:
            return reservation
        if operation_type == "entry":
            self.transition_signal(signal_id, SignalStatus.RESERVED, actor="strategy")
        return reservation

    def transition_signal(
        self,
        signal_id: str,
        target_status: SignalStatus,
        actor: str,
    ) -> SignalTransitionResult:
        signal = self.repository.get(signal_id)
        if not signal:
            return SignalTransitionResult(
                ok=False,
                signal_id=signal_id,
                status=SignalStatus.FAILED,
                reason="signal_missing",
            )

        updated = self.repository.transition_status(
            signal_id,
            target_status,
            actor,
            ALLOWED_TRANSITIONS,
        )
        if not updated:
            return SignalTransitionResult(
                ok=False,
                signal_id=signal_id,
                status=signal.status,
                reason="invalid_transition",
            )
        return SignalTransitionResult(ok=True, signal_id=signal_id, status=target_status)

    def list_events(self, signal_id: str) -> list[SignalLifecycleEvent]:
        return self.repository.list_events(signal_id)


def _signal_received_at(signal: ParsedSignal) -> datetime:
    received_at = signal.received_at
    if received_at.tzinfo is None:
        return received_at.replace(tzinfo=timezone.utc)
    return received_at.astimezone(timezone.utc)
