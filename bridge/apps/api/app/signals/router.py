from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
import os
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Body, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from app.contracts.message_processing import MessageProcessingRecord, MessageProcessingStatus
from app.db.models_message_processing import MessageProcessingEvent, MessageProcessingTransitionResult
from app.risk import router as risk_router
from app.dependencies import require_bearer_user
from app.services.message_processing import MessageProcessingStore
from app.services.signal_parser import ParsedSignal, SignalStatus
from app.services.signal_store import SignalStore


router = APIRouter()
_message_store = MessageProcessingStore()
_signal_store = SignalStore()
_proposal_store: dict[str, dict] = {}
DEFAULT_SIGNAL_MEDIA_DIRS = "/data/media:fixtures/signals/telegram_latest20/media"


def reset_message_processing_store() -> None:
    global _message_store, _signal_store, _proposal_store

    _message_store = MessageProcessingStore()
    _signal_store = SignalStore()
    _proposal_store = {}


@router.get("/status")
def status() -> dict[str, str]:
    return {"status": "mounted"}


@router.post("/messages")
def upsert_message(
    request: Request,
    body: object = Body(default=False),
    authorization: str | None = Header(default=None),
) -> dict:
    _require_write_user(authorization)
    payload = _require_payload(body)
    message_id = _require_string(payload, "message_id")
    source = _optional_string(payload, "source", "telegram")
    channel_id = _optional_string(payload, "channel_id", "")
    signal_id = _optional_string(payload, "signal_id", "")
    metadata = _metadata(payload)

    record = MessageProcessingRecord(
        message_id=message_id,
        source=source,
        channel_id=channel_id,
        signal_id=signal_id,
        metadata=metadata,
    )
    stored_record = _store(request).upsert_message(record)
    return _serialize_record(stored_record)


@router.post("/messages/{message_id}/status")
def transition_message_status(
    request: Request,
    message_id: str,
    body: object = Body(default=False),
    authorization: str | None = Header(default=None),
) -> dict:
    _require_write_user(authorization)
    payload = _require_payload(body)
    status_value = _require_string(payload, "status")

    try:
        target_status = MessageProcessingStatus(status_value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="status invalid") from exc

    actor = _optional_string(payload, "actor", "api")
    reason = _optional_string(payload, "reason", "")
    result = _store(request).transition_message(
        message_id,
        target_status,
        actor=actor,
        reason=reason,
    )
    if not result.ok:
        return JSONResponse(
            status_code=422,
            content={"result": _serialize_result(result)},
        )

    _patch_lifecycle_fields(request, message_id, payload)
    record = _store(request).get_message(message_id)
    return {
        "ok": result.ok,
        "message_id": result.message_id,
        "status": result.status.value,
        "reason": reason,
        "record": _serialize_record(record),
    }


@router.get("/messages/{message_id}")
def get_message(
    request: Request,
    message_id: str,
    authorization: str | None = Header(default=None),
) -> dict:
    require_bearer_user(authorization)
    try:
        record = _store(request).get_message(message_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="message missing") from exc
    return _serialize_record(record)


@router.get("/messages/{message_id}/events")
def get_message_events(
    request: Request,
    message_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, list[dict]]:
    require_bearer_user(authorization)
    events = _store(request).list_events(message_id)
    return {"events": [_serialize_event(event) for event in events]}


@router.get("/{signal_id}/message-events")
def get_signal_message_events(
    request: Request,
    signal_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, list[dict]]:
    require_bearer_user(authorization)
    events = _store(request).list_events_by_signal_id(signal_id)
    return {"events": [_serialize_event(event) for event in events]}


@router.get("/{signal_id}/media/{index}")
def get_signal_media(
    request: Request,
    signal_id: str,
    index: int,
    authorization: str | None = Header(default=None),
) -> FileResponse:
    require_bearer_user(authorization)
    signal = _get_signal_or_404(request, signal_id)
    if index < 0 or index >= len(signal.media):
        raise HTTPException(status_code=404, detail="media missing")

    media_item = signal.media[index]
    raw_path = getattr(media_item, "path", "")
    filename = os.path.basename(str(raw_path).replace("\\", os.sep))
    if not filename:
        raise HTTPException(status_code=404, detail="media missing")

    media_path = _find_signal_media_file(filename)
    if media_path is None:
        raise HTTPException(status_code=404, detail="media missing")

    mime_type = str(getattr(media_item, "mime_type", "") or "application/octet-stream")
    return FileResponse(media_path, media_type=mime_type)


@router.get("/{signal_id}")
def get_signal_detail(
    request: Request,
    signal_id: str,
    authorization: str | None = Header(default=None),
) -> dict:
    require_bearer_user(authorization)
    return _serialize_signal(_get_signal_or_404(request, signal_id))


@router.get("/review/queue")
def review_queue(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, list[dict]]:
    require_bearer_user(authorization)
    store = _review_store(request)
    signals = store.repository.list_by_status(SignalStatus.NEEDS_REVIEW)
    return {
        "signals": [_serialize_signal(signal) for signal in signals],
        "proposals": _review_proposals(signals),
    }


@router.post("/review/{signal_id}/approve")
def approve_signal_review(
    request: Request,
    signal_id: str,
    body: object = Body(default=False),
    authorization: str | None = Header(default=None),
) -> dict:
    return _decide_signal_review(
        request=request,
        signal_id=signal_id,
        body=body,
        authorization=authorization,
        target_status=SignalStatus.APPROVED,
        event_type="signal_review_approved",
    )


@router.post("/review/{signal_id}/reject")
def reject_signal_review(
    request: Request,
    signal_id: str,
    body: object = Body(default=False),
    authorization: str | None = Header(default=None),
) -> dict:
    return _decide_signal_review(
        request=request,
        signal_id=signal_id,
        body=body,
        authorization=authorization,
        target_status=SignalStatus.REJECTED,
        event_type="signal_review_rejected",
    )


@router.post("/review/proposals")
def upsert_review_proposal(
    body: object = Body(default=False),
    authorization: str | None = Header(default=None),
) -> dict:
    _require_write_user(authorization)
    payload = _require_payload(body)
    proposal_id = _optional_string(payload, "proposal_id", "")
    if not proposal_id:
        proposal_id = f"proposal-{uuid4()}"
    proposal_type = _require_string(payload, "type")
    signal_id = _optional_string(payload, "signal_id", "")
    title = _optional_string(payload, "title", proposal_type)
    detail = _optional_string(payload, "detail", "")
    proposed_action = _optional_string(payload, "proposed_action", proposal_type)
    classification = payload.get("classification", {})
    if not isinstance(classification, dict):
        raise HTTPException(status_code=422, detail="classification must be an object")

    proposal = {
        "proposal_id": proposal_id,
        "type": proposal_type,
        "signal_id": signal_id,
        "title": title,
        "detail": detail,
        "proposed_action": proposed_action,
        "classification": classification,
        "status": "open",
    }
    _proposal_store[proposal_id] = proposal
    return proposal


@router.post("/review/proposals/{proposal_id}/approve")
def approve_review_proposal(
    request: Request,
    proposal_id: str,
    body: object = Body(default=False),
    authorization: str | None = Header(default=None),
) -> dict:
    return _decide_review_proposal(
        request=request,
        proposal_id=proposal_id,
        body=body,
        authorization=authorization,
        decision="approved",
        event_type="signal_proposal_approved",
    )


@router.post("/review/proposals/{proposal_id}/reject")
def reject_review_proposal(
    request: Request,
    proposal_id: str,
    body: object = Body(default=False),
    authorization: str | None = Header(default=None),
) -> dict:
    return _decide_review_proposal(
        request=request,
        proposal_id=proposal_id,
        body=body,
        authorization=authorization,
        decision="rejected",
        event_type="signal_proposal_rejected",
    )


def _serialize_signal(signal: ParsedSignal) -> dict:
    data = asdict(signal)
    data["status"] = signal.status.value
    data["received_at"] = _serialize_value(signal.received_at)
    data["entry"] = _serialize_value(signal.entry)
    data["media"] = _serialize_value(signal.media)
    data["media_metadata"] = _media_metadata(signal.media)
    data["take_profits"] = _serialize_value(signal.take_profits)
    data["leverage"] = _serialize_value(signal.leverage)
    data["classification"] = _signal_classification(signal)
    return data


def _media_metadata(media_items: list) -> dict:
    return {
        "count": len(media_items),
        "items": [
            {
                "index": index,
                "mime_type": str(getattr(item, "mime_type", "") or ""),
            }
            for index, item in enumerate(media_items)
        ],
    }


def _find_signal_media_file(filename: str) -> Path | None:
    for directory in _signal_media_dirs():
        media_path = directory / filename
        if media_path.is_file():
            return media_path
    return None


def _signal_media_dirs() -> list[Path]:
    dirs_value = os.environ.get("SIGNAL_MEDIA_DIRS", DEFAULT_SIGNAL_MEDIA_DIRS)
    return [Path(item) for item in dirs_value.split(":") if item]


def _get_signal_or_404(request: Request, signal_id: str) -> ParsedSignal:
    try:
        return _review_store(request).get_signal(signal_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="signal missing") from exc


def _serialize_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return _serialize_value(asdict(value))
    if isinstance(value, list):
        return [_serialize_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _serialize_value(item) for key, item in value.items()}
    if hasattr(value, "value"):
        return getattr(value, "value")
    return value


def _signal_classification(signal: ParsedSignal) -> dict:
    reason_codes = list(signal.review_reason_codes)
    conclusion = "requires_human_review" if reason_codes else "review_requested"
    confidence = "medium" if reason_codes else "low"
    return {
        "engine": "hermes",
        "conclusion": conclusion,
        "confidence": confidence,
        "reason_codes": reason_codes,
        "take_profit_parse_status": signal.take_profit_parse_status,
        "proposal_types": _proposal_types_for_signal(signal),
    }


def _proposal_types_for_signal(signal: ParsedSignal) -> list[str]:
    proposal_types = ["approve_request"]
    if any("rate" in reason.lower() or "limit" in reason.lower() for reason in signal.review_reason_codes):
        proposal_types.append("rate_limit_adjustment")
    return proposal_types


def _review_proposals(signals: list[ParsedSignal]) -> list[dict]:
    proposals = [proposal for proposal in _proposal_store.values() if proposal.get("status") == "open"]
    derived = []
    existing_ids = {str(proposal.get("proposal_id", "")) for proposal in proposals}
    for signal in signals:
        for proposal_type in _proposal_types_for_signal(signal):
            proposal_id = f"{signal.signal_id}:{proposal_type}"
            if proposal_id in existing_ids:
                continue
            derived.append(
                {
                    "proposal_id": proposal_id,
                    "type": proposal_type,
                    "signal_id": signal.signal_id,
                    "title": _proposal_title(proposal_type),
                    "detail": ", ".join(signal.review_reason_codes) or signal.raw_text[:120],
                    "proposed_action": proposal_type,
                    "classification": _signal_classification(signal),
                    "status": "open",
                }
            )
    return proposals + derived


def _proposal_title(proposal_type: str) -> str:
    if proposal_type == "rate_limit_adjustment":
        return "Rate-limit adjustment request"
    return "Approve request"


def _decide_signal_review(
    request: Request,
    signal_id: str,
    body: object,
    authorization: str | None,
    target_status: SignalStatus,
    event_type: str,
) -> dict:
    user = _require_write_user(authorization)
    payload = _require_payload(body)
    reason = _require_string(payload, "reason")
    actor = _optional_string(payload, "actor", user["actor_id"])
    result = _review_store(request).transition_signal(signal_id, target_status, actor=actor)
    audit_result = "success" if result.ok else "failure"
    _record_signal_audit(
        request=request,
        user=user,
        event_type=event_type,
        signal_id=signal_id,
        reason=reason,
        payload={"signal_id": signal_id, "target_status": target_status.value, "result": _serialize_signal_result(result)},
        result=audit_result,
    )
    if not result.ok:
        return JSONResponse(status_code=422, content={"result": _serialize_signal_result(result)})
    return {
        "ok": result.ok,
        "signal_id": result.signal_id,
        "status": result.status.value,
        "reason": reason,
    }


def _decide_review_proposal(
    request: Request,
    proposal_id: str,
    body: object,
    authorization: str | None,
    decision: str,
    event_type: str,
) -> dict:
    user = _require_write_user(authorization)
    payload = _require_payload(body)
    reason = _require_string(payload, "reason")
    proposal = _proposal_store.get(proposal_id)
    if not proposal:
        proposal = _derived_proposal_by_id(request, proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="proposal missing")

    decided = dict(proposal)
    decided["status"] = decision
    decided["decision_reason"] = reason
    _proposal_store[proposal_id] = decided
    _record_signal_audit(
        request=request,
        user=user,
        event_type=event_type,
        signal_id=str(decided.get("signal_id", proposal_id)),
        reason=reason,
        payload={"proposal_id": proposal_id, "decision": decision, "proposal": decided},
        result="success",
    )
    return {"ok": True, "proposal": decided}


def _derived_proposal_by_id(request: Request, proposal_id: str) -> dict | bool:
    signals = _review_store(request).repository.list_by_status(SignalStatus.NEEDS_REVIEW)
    for proposal in _review_proposals(signals):
        if proposal.get("proposal_id") == proposal_id:
            return proposal
    return False


def _serialize_signal_result(result) -> dict:
    return {
        "ok": result.ok,
        "signal_id": result.signal_id,
        "status": result.status.value,
        "reason": result.reason,
    }


def _record_signal_audit(
    request: Request,
    user: dict,
    event_type: str,
    signal_id: str,
    reason: str,
    payload: dict,
    result: str,
) -> dict:
    request_id = getattr(request.state, "request_id", "")
    if not request_id:
        request_id = request.headers.get("x-request-id", "")
    if not request_id:
        request_id = str(uuid4())
    return risk_router.audit_log.record(
        event_type=event_type,
        actor_id=user["actor_id"],
        actor_role=user["role"],
        request_id=request_id,
        correlation_id=signal_id,
        reason=reason,
        payload=payload,
        result=result,
    )


def _review_store(request: Request) -> SignalStore:
    store = getattr(request.app.state, "signal_store", False)
    if store:
        return store
    return _signal_store


def _serialize_record(record: MessageProcessingRecord) -> dict:
    data = asdict(record)
    data["status"] = record.status.value
    return data


def _serialize_event(event: MessageProcessingEvent) -> dict:
    data = asdict(event)
    from_status = event.from_status
    if isinstance(from_status, MessageProcessingStatus):
        data["from_status"] = from_status.value
    data["to_status"] = event.to_status.value
    return data


def _serialize_result(result: MessageProcessingTransitionResult) -> dict:
    return {
        "ok": result.ok,
        "message_id": result.message_id,
        "status": result.status.value,
        "reason": result.reason,
    }


def _patch_lifecycle_fields(request: Request, message_id: str, body: dict) -> None:
    updates = {}
    for field in ["cron_job_id", "output_path", "error"]:
        value = _optional_string(body, field, "")
        if value:
            updates[field] = value

    if not updates:
        return

    _store(request).update_lifecycle_fields(message_id, updates)


def _store(request: Request) -> MessageProcessingStore:
    store = getattr(request.app.state, "message_processing_store", False)
    if store:
        return store
    return _message_store


def _require_write_user(authorization: str | None) -> dict:
    user = require_bearer_user(authorization)
    role = user["role"]
    if role not in {"trader", "risk_admin"}:
        raise HTTPException(status_code=403, detail="permission denied")
    return user


def _require_payload(body: object) -> dict:
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be an object")
    return body


def _require_string(body: dict, field: str) -> str:
    value = body.get(field, False)
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail=f"{field} must be a string")
    if not value:
        raise HTTPException(status_code=422, detail=f"{field} required")
    return value


def _optional_string(body: dict, field: str, default: str) -> str:
    value = body.get(field, default)
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail=f"{field} must be a string")
    return value


def _metadata(body: dict) -> dict:
    metadata = body.get("metadata", {})
    if not isinstance(metadata, dict):
        raise HTTPException(status_code=422, detail="metadata must be an object")
    return metadata
