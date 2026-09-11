"""原因码（contracts/research-schema.md §4，冻结后只增不改）与严重度。"""
from __future__ import annotations

from enum import StrEnum


class Reason(StrEnum):
    RAW_HASH_MISMATCH = "RAW_HASH_MISMATCH"
    SCHEMA_DRIFT = "SCHEMA_DRIFT"
    TIME_UNIT_INVALID = "TIME_UNIT_INVALID"
    VERSION_TIME_UNKNOWN = "VERSION_TIME_UNKNOWN"
    MEDIA_MISSING = "MEDIA_MISSING"
    OCR_UNREADABLE = "OCR_UNREADABLE"
    DUPLICATE_EXACT = "DUPLICATE_EXACT"
    COPY_LINK_AMBIGUOUS = "COPY_LINK_AMBIGUOUS"
    NOT_SIGNAL = "NOT_SIGNAL"
    INTENT_AMBIGUOUS = "INTENT_AMBIGUOUS"
    SYMBOL_TIME_INVALID = "SYMBOL_TIME_INVALID"
    UNIT_SCALE_CONFLICT = "UNIT_SCALE_CONFLICT"
    TEXT_IMAGE_CONFLICT = "TEXT_IMAGE_CONFLICT"
    ENTRY_LINK_AMBIGUOUS = "ENTRY_LINK_AMBIGUOUS"
    PARENT_MISSING = "PARENT_MISSING"
    LIFECYCLE_INVALID = "LIFECYCLE_INVALID"
    DEPENDENCY_NOT_AVAILABLE = "DEPENDENCY_NOT_AVAILABLE"
    BAR_GAP = "BAR_GAP"
    KEY_DUPLICATE_OR_ORDER = "KEY_DUPLICATE_OR_ORDER"
    OHLC_INVALID = "OHLC_INVALID"
    PRICE_SPIKE_FLAG = "PRICE_SPIKE_FLAG"
    FUNDING_SCHEDULE_GAP = "FUNDING_SCHEDULE_GAP"
    RULE_HISTORY_MISSING = "RULE_HISTORY_MISSING"
    MARK_STALE = "MARK_STALE"
    ENTRY_MARK_DEVIATION = "ENTRY_MARK_DEVIATION"
    VOL_HISTORY_SHORT = "VOL_HISTORY_SHORT"
    LABEL_RIGHT_CENSORED = "LABEL_RIGHT_CENSORED"
    CONSENT_REVOKED = "CONSENT_REVOKED"
    EDIT_ORIGINAL_UNAVAILABLE = "EDIT_ORIGINAL_UNAVAILABLE"  # 契约 v1 §9.1 新增（H1：仅最终编辑版可见，原始入场隔离），一般


#: 致命类：契约 §4 原文三项（方向/品种/归属/数量级错）。RAW_HASH_MISMATCH 是"停批"动作而非严重度，按契约归一般。
FATAL: frozenset[Reason] = frozenset({Reason.UNIT_SCALE_CONFLICT, Reason.ENTRY_LINK_AMBIGUOUS, Reason.SYMBOL_TIME_INVALID})

#: 主原因互斥计数的冻结优先级（越靠前越优先）；合并稿 C.3「主原因按冻结优先级互斥计数」
PRIMARY_PRIORITY: tuple[Reason, ...] = (
    Reason.RAW_HASH_MISMATCH,
    Reason.SCHEMA_DRIFT,
    Reason.CONSENT_REVOKED,
    Reason.TIME_UNIT_INVALID,
    Reason.KEY_DUPLICATE_OR_ORDER,
    Reason.VERSION_TIME_UNKNOWN,
    Reason.EDIT_ORIGINAL_UNAVAILABLE,
    Reason.UNIT_SCALE_CONFLICT,
    Reason.SYMBOL_TIME_INVALID,
    Reason.ENTRY_LINK_AMBIGUOUS,
    Reason.TEXT_IMAGE_CONFLICT,
    Reason.MEDIA_MISSING,
    Reason.OCR_UNREADABLE,
    Reason.DUPLICATE_EXACT,
    Reason.COPY_LINK_AMBIGUOUS,
    Reason.INTENT_AMBIGUOUS,
    Reason.PARENT_MISSING,
    Reason.LIFECYCLE_INVALID,
    Reason.DEPENDENCY_NOT_AVAILABLE,
    Reason.MARK_STALE,
    Reason.ENTRY_MARK_DEVIATION,
    Reason.VOL_HISTORY_SHORT,
    Reason.LABEL_RIGHT_CENSORED,
    Reason.NOT_SIGNAL,
    Reason.BAR_GAP,
    Reason.OHLC_INVALID,
    Reason.PRICE_SPIKE_FLAG,
    Reason.FUNDING_SCHEDULE_GAP,
    Reason.RULE_HISTORY_MISSING,
)
_RANK = {r: i for i, r in enumerate(PRIMARY_PRIORITY)}


def severity(reason: Reason | str) -> str:
    return "fatal" if Reason(reason) in FATAL else "general"


def severity_of_all(reasons: list[str]) -> str:
    """一组原因码的严重度 = 最重者（任一致命即致命；review-G1-P1 S07）。"""
    return "fatal" if any(Reason(r) in FATAL for r in reasons) else "general"


def primary_reason(reasons: list[str]) -> str | None:
    """主原因（互斥计数用）：先严重度（致命优先），再冻结规则优先级（S07）。"""
    if not reasons:
        return None
    return min(reasons, key=lambda r: (0 if Reason(r) in FATAL else 1, _RANK.get(Reason(r), 10**6)))
