"""LLM / OCR 抽取与裁决接口（ADR-G1 §5；GOAL-1 §1；review-G1-P1 S13）。

- 纯接口 + 录制回放 provider；**真实调用 gated**：默认 `NoNetworkClient` 拒绝一切调用，只有环境变量 `QUANT_LAB_ALLOW_LLM=1`
  且显式传入真实 client 才可能联网（本仓库不含真实 provider 实现）。任何 provider 都经 `gate()` 统一入口。
- 抽取输出必须带证据：每个数字字段有 span 且 `text[start:end]` 解析后等于该数字，否则整条拒收 → Abstention；非法类型同样拒收（不抛异常）。
- OCR：`OcrProvider` 协议；`RecordedOcr` 按 media sha256 回放 {text, numbers[{value,bbox}]}；`NoOcr` 返回 not_run（不等于一致）。
- 裁决：`adjudicate()` 输入候选边与有界上下文；输出 action∈{link,new_episode,unresolved}，selected 必须来自输入候选，
  evidence_message_ids 必须是输入上下文；一次扩展、传输/格式失败最多两次重试；超预算 → unresolved。
- usage 计量：录制回放 usage 标 synthetic=True，不伪报真实成本。
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
import os
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .lake import q12

PROMPT_VERSION = "extract-v1"
ADJ_PROMPT_VERSION = "adjudicate-v1"
SCHEMA_NAME_EXTRACT = "extracted_event.v1"
SCHEMA_NAME_ADJ = "adjudication.v1"
CONTEXT_BUDGET = {"candidates": 20, "messages": 32, "images": 8, "tokens": 8000}
CONTEXT_BUDGET_EXTENDED = {"candidates": 20, "messages": 64, "images": 16, "tokens": 16000}
MAX_RETRIES = 2

SYSTEM_EXTRACT = (
    "你是交易频道消息的结构化抽取器。只输出一个 JSON 对象，不要任何解释。"
    "字段：kind(entry_proposal|amend|cancel|expire|entry_claimed|add|reduce|stop_move|tp_ladder|close_claimed|correction|delete_notice|analysis|result_post|chatter|undecidable),"
    " symbol_raw, side(long|short|null), entry{lo,hi,kind(limit|zone|market_ref|ladder)}|null, entries[数字], stop|null, tps[{level}], "
    "spans[{field,start,end}]：每个数字字段必须给出它在原文中的字符区间，区间内文本必须与该数字原样一致。"
    "无法判断时 kind=undecidable 并给 reason_codes。不得推断原文没有的数字。"
)
SYSTEM_ADJ = (
    "你是交易频道消息归属裁决器。只输出一个 JSON 对象：action(link|new_episode|unresolved)、selected_candidate_id、"
    "evidence_message_ids[{peer_id,message_id,source_version_id}]、rejected_candidate_ids[]、reason_codes[]、needs_more_context(bool)。"
    "selected 必须来自输入候选；证据必须来自输入上下文；不得创造目标。"
)


def record_key(system: str, user: str, schema_name: str) -> str:
    return hashlib.sha256(f"{system}\n\x00{user}\n\x00{schema_name}".encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Abstention:
    reason_code: str
    note: str = ""


@dataclass
class LLMResponse:
    payload: dict[str, Any]
    usage: dict[str, Any] = field(default_factory=dict)
    provider: str = "recorded"
    model: str = "recorded"
    response_hash: str = ""


class TransportError(RuntimeError):
    """传输/格式失败（可重试，最多 MAX_RETRIES）。"""


@runtime_checkable
class LLMClient(Protocol):
    name: str
    version: str

    def complete_json(self, *, system: str, user: str, schema_name: str) -> LLMResponse | Abstention: ...


def gate(client: LLMClient | None) -> LLMClient:
    """统一审批入口：None → NoNetworkClient；非录制 client 必须有 QUANT_LAB_ALLOW_LLM=1。"""
    if client is None:
        return NoNetworkClient()
    if getattr(client, "name", "") in ("recorded", "no_network"):
        return client
    if os.environ.get("QUANT_LAB_ALLOW_LLM") != "1":
        raise PermissionError(f"真实 LLM provider {getattr(client, 'name', '?')} 被闸门拒绝：未设置 QUANT_LAB_ALLOW_LLM=1（API 预算与云上传范围未拍板）")
    return client


class NoNetworkClient:
    name = "no_network"
    version = "gated"

    def complete_json(self, *, system: str, user: str, schema_name: str) -> LLMResponse | Abstention:
        if os.environ.get("QUANT_LAB_ALLOW_LLM") == "1":
            raise NotImplementedError("真实 LLM provider 未在本仓库实现：等 API 预算与云上传范围闸门后另接入")
        return Abstention("INTENT_AMBIGUOUS", "llm_gated:no_network")


class RecordedClient:
    """夹具回放：按 record_key 查表；未命中抛 KeyError（不静默回落到真实调用）。记录 {abstain} 或 {transport_error} 也可回放。"""

    name = "recorded"

    def __init__(self, fixtures: dict[str, dict[str, Any]], version: str = "fixture-v1", model: str = "synthetic-recorded") -> None:
        self._fixtures = fixtures
        self.version = version
        self.model = model
        self.calls = 0

    @classmethod
    def from_file(cls, path: str | os.PathLike) -> "RecordedClient":
        doc = json.loads(pathlib.Path(path).read_text(encoding="utf-8"), parse_float=Decimal)  # W02：经济量无损读取，不经 float
        return cls(doc["items"], version=doc.get("version", "fixture-v1"), model=doc.get("model", "synthetic-recorded"))

    def has(self, *, system: str, user: str, schema_name: str) -> bool:
        return record_key(system, user, schema_name) in self._fixtures

    def complete_json(self, *, system: str, user: str, schema_name: str) -> LLMResponse | Abstention:
        key = record_key(system, user, schema_name)
        if key not in self._fixtures:
            raise KeyError(f"录制夹具缺 key={key[:12]}…（schema={schema_name}）")
        self.calls += 1
        rec = self._fixtures[key]
        if rec.get("transport_error"):
            n = rec["transport_error"].get("times", 1)
            rec["transport_error"]["times"] = n - 1
            if n > 0:
                raise TransportError(rec["transport_error"].get("note", "recorded transport error"))
        if rec.get("abstain"):
            return Abstention(rec["abstain"].get("reason_code", "INTENT_AMBIGUOUS"), rec["abstain"].get("note", ""))
        payload = rec["response"]
        return LLMResponse(payload=payload, usage={**rec.get("usage", {}), "synthetic": True}, provider="recorded", model=self.model,
                           response_hash=hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest())


def call_with_retry(client: LLMClient, *, system: str, user: str, schema_name: str) -> tuple[LLMResponse | Abstention, int]:
    """传输/格式失败最多重试 MAX_RETRIES 次；语义拒答不重试。返回 (结果, 尝试次数)。"""
    attempt = 0
    while True:
        attempt += 1
        try:
            return client.complete_json(system=system, user=user, schema_name=schema_name), attempt
        except TransportError as e:
            if attempt > MAX_RETRIES:
                return Abstention("INTENT_AMBIGUOUS", f"transport_failed_after_{attempt}:{e}"), attempt


def build_extract_prompt(text: str, *, channel_name: str, message_date: str | None) -> tuple[str, str]:
    user = json.dumps({"channel": channel_name, "date": message_date, "text": text, "prompt_version": PROMPT_VERSION}, ensure_ascii=False)
    return SYSTEM_EXTRACT, user


NUMERIC_FIELDS = ("entry.lo", "entry.hi", "stop", "tps", "entries")


def _num(v: Any) -> Decimal | None:
    """数值证据全程 Decimal（A18/W01）。接受 Decimal / int / 数字字符串；float 是有损载体 → 拒收（返回 None）。"""
    if isinstance(v, bool) or v is None or isinstance(v, float):
        return None
    if isinstance(v, Decimal):
        return v if v.is_finite() else None
    if isinstance(v, int):
        return Decimal(v)
    if isinstance(v, str):
        try:
            d = Decimal(v.replace(",", "").strip())
        except (InvalidOperation, ValueError):
            return None
        return d if d.is_finite() else None
    return None


def validate_evidence(payload: dict[str, Any], text: str) -> list[str]:
    """校验 LLM 抽取输出：类型合法 + 每个数字字段有 span 且 text[start:end] 解析后等于该数字。返回错误列表（空=通过）。不抛异常。"""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["payload_not_object"]
    spans = payload.get("spans")
    if spans is None:
        spans = []
    if not isinstance(spans, list):
        return ["spans_not_list"]
    by_field: dict[str, list[tuple[int, int]]] = {}
    for s in spans:
        if not isinstance(s, dict):
            errors.append(f"bad_span:{s!r}")
            continue
        try:
            f, a, b = str(s["field"]), int(s["start"]), int(s["end"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"bad_span:{s!r}")
            continue
        if a < 0 or b > len(text) or a >= b:
            errors.append(f"span_out_of_range:{f}:{a}-{b}")
            continue
        by_field.setdefault(f, []).append((a, b))

    def claimed(field_name: str) -> list[float] | None:
        try:
            if field_name == "stop":
                v = payload.get("stop")
                if v is None:
                    return []
                n = _num(v)
                return None if n is None else [n]
            if field_name in ("entry.lo", "entry.hi"):
                e = payload.get("entry")
                if e is None:
                    return []
                if not isinstance(e, dict):
                    return None
                v = e.get(field_name.split(".")[1])
                if v is None:
                    return []
                n = _num(v)
                return None if n is None else [n]
            if field_name == "tps":
                out = []
                for t in payload.get("tps") or []:
                    n = _num(t.get("level") if isinstance(t, dict) else t)
                    if n is None:
                        return None
                    out.append(n)
                return out
            if field_name == "entries":
                out = []
                for x in payload.get("entries") or []:
                    n = _num(x)
                    if n is None:
                        return None
                    out.append(n)
                return out
        except (AttributeError, TypeError):
            return None
        return []

    from .extract import parse_number_token  # 延迟导入避免环

    for f in NUMERIC_FIELDS:
        values = claimed(f)
        if values is None:
            errors.append(f"bad_type:{f}")
            continue
        if not values:
            continue
        sp = by_field.get(f, [])
        if len(sp) < len(values):
            errors.append(f"missing_span:{f}:{len(sp)}/{len(values)}")
            continue
        for v, (a, b) in zip(values, sp):
            tok = parse_number_token(text[a:b])
            if tok is None or abs(tok - Decimal(str(v))) > Decimal("0.000000001") * max(Decimal(1), abs(Decimal(str(v)))):
                errors.append(f"span_mismatch:{f}:{text[a:b]!r}!={v}")
    sh = payload.get("size_hint")
    if sh is not None and (not isinstance(sh, dict) or any(k in sh and sh[k] is not None and _num(sh[k]) is None for k in ("qty", "fraction", "leverage", "notional"))):
        errors.append("bad_type:size_hint")
    if isinstance(sh, dict):
        for key in ("qty", "fraction", "leverage", "notional"):
            value = sh.get(key)
            if value is None:
                continue
            number = _num(value)
            if number is None or number <= 0 or (key == "fraction" and number > 1):
                errors.append(f"bad_value:size_hint.{key}")
                continue
            field_name = f"size_hint.{key}"
            evidence = by_field.get(field_name, [])
            matched = False
            for start, end in evidence:
                token = text[start:end].strip()
                if key == "fraction":
                    token = token.rstrip("%％")
                parsed = parse_number_token(token)
                if key == "fraction" and parsed is not None:
                    parsed /= 100
                if parsed is not None and q12(parsed) == q12(number):  # 定标度 12 逐字相等（A18）
                    matched = True
            if not matched:
                errors.append(f"missing_or_invalid_span:{field_name}")
    ex = payload.get("expires_after_s")
    if ex is not None:
        value = _num(ex)
        if value is None or value <= 0:
            errors.append("bad_type:expires_after_s")
        matches = []
        for start, end in by_field.get("expires_after_s", []) + by_field.get("expiry", []):
            token = text[start:end]
            match = re.fullmatch(r"\s*(\d+)\s*(秒|seconds?|分钟|minutes?|小时|hours?)\s*", token, re.I)
            if match:
                factor = 1
                if re.fullmatch(r"分钟|minutes?", match[2], re.I):
                    factor = 60
                if re.fullmatch(r"小时|hours?", match[2], re.I):
                    factor = 3600
                matches.append(int(match[1]) * factor)
        if value not in matches:
            errors.append("missing_or_invalid_span:expires_after_s")

    return errors


# ---------------------------------------------------------------- OCR
@dataclass
class OcrResult:
    status: str  # ok | unreadable | not_run
    text: str = ""
    numbers: list[dict[str, Any]] = field(default_factory=list)  # [{value, bbox:[x0,y0,x1,y1]}]
    provider: str = "none"
    version: str = ""


@runtime_checkable
class OcrProvider(Protocol):
    name: str
    version: str

    def read(self, media_hash: str) -> OcrResult: ...


class NoOcr:
    name = "no_ocr"
    version = "not_run"

    def read(self, media_hash: str) -> OcrResult:
        return OcrResult("not_run", provider=self.name, version=self.version)


class RecordedOcr:
    """按 media sha256 回放录制 OCR；未录制 → unreadable（拒答，不猜）。"""

    name = "recorded_ocr"

    def __init__(self, items: dict[str, dict[str, Any]], version: str = "ocr-fixture-v1") -> None:
        self._items = items
        self.version = version

    @classmethod
    def from_file(cls, path: str | os.PathLike) -> "RecordedOcr":
        doc = json.loads(pathlib.Path(path).read_text(encoding="utf-8"), parse_float=Decimal)  # W02
        return cls(doc["items"], version=doc.get("version", "ocr-fixture-v1"))

    def read(self, media_hash: str) -> OcrResult:
        rec = self._items.get(media_hash)
        if rec is None or rec.get("unreadable"):
            return OcrResult("unreadable", provider=self.name, version=self.version)
        size = rec.get("size")
        if size is not None and not valid_size(size):
            return OcrResult("unreadable", provider=self.name, version=self.version)  # 尺寸记录损坏：有界拒答，不中断（U01）
        nums = [n for n in rec.get("numbers", []) if valid_ocr_number(n, size)]
        return OcrResult("ok", text=rec.get("text", ""), numbers=nums, provider=self.name, version=self.version)


def valid_size(size: Any) -> bool:
    """图片尺寸记录：恰两项有限正数。"""
    if not isinstance(size, (list, tuple)) or len(size) != 2:
        return False
    try:
        w, h = float(size[0]), float(size[1])
    except (TypeError, ValueError):
        return False
    return all(v == v and abs(v) != float("inf") and v > 0 for v in (w, h))


def _num_ocr(v: Any) -> Decimal | None:
    """OCR 数字记录的值：允许 float 载体（OCR 值之后必须与图中/原文 token 逐字相等才算证据，有损值自然匹配不上，不会伪造证据）。"""
    if isinstance(v, float):
        return Decimal(str(v)) if v == v and abs(v) != float("inf") else None
    return _num(v)


def valid_ocr_number(n: Any, size: list[int] | tuple[int, int] | None) -> bool:
    """OCR 数字记录合法性（T03）：value 有限数；bbox 若给出必须 4 元有限非负、x0<x1、y0<y1，且落在图片尺寸内（已知尺寸时）。非法 → 丢弃该记录，不中断。"""
    if not isinstance(n, dict) or _num_ocr(n.get("value")) is None:
        return False
    b = n.get("bbox")
    if b is None:
        return False
    if not isinstance(b, (list, tuple)) or len(b) != 4:
        return False
    try:
        x0, y0, x1, y1 = (float(v) for v in b)
    except (TypeError, ValueError):
        return False
    if any(v != v or abs(v) == float("inf") or v < 0 for v in (x0, y0, x1, y1)) or not (x0 < x1 and y0 < y1):
        return False
    if size is not None:
        if not valid_size(size):
            return False
        if x1 > float(size[0]) or y1 > float(size[1]):
            return False
    return True


# ---------------------------------------------------------------- 裁决
@dataclass
class AdjudicationRequest:
    request_id: str
    event_plan_id: str
    candidates: list[dict[str, Any]]  # [{candidate_id, to_plan_id, method, strength, evidence}]
    context: list[dict[str, Any]]  # [{peer_id, message_id, source_version_id, text}]（已按截止 as-of 过滤）
    decision_cutoff: str | None
    purpose: str = "description"  # description | decision
    extended: bool = False


def sent_subsets(req: AdjudicationRequest) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """实际进入提示的候选与上下文（按预算截断）——校验只能对照这两个集合。"""
    budget = CONTEXT_BUDGET_EXTENDED if req.extended else CONTEXT_BUDGET
    candidates = req.candidates[: budget["candidates"]]
    context = []
    # UTF-8 bytes upper-bound tokenizer pieces conservatively; no network tokenizer needed.
    remaining = budget["tokens"] - len(json.dumps(candidates, ensure_ascii=False).encode("utf-8")) - 1024
    for message in req.context[: budget["messages"]]:
        entry = dict(message)
        raw = str(entry.get("text", "")).encode("utf-8")
        overhead = len(json.dumps({k: v for k, v in entry.items() if k != "text"}, default=str).encode("utf-8")) + 32
        if remaining <= overhead:
            break
        entry["text"] = raw[:remaining - overhead].decode("utf-8", errors="ignore")
        context.append(entry)
        remaining -= len(json.dumps(entry, ensure_ascii=False, default=str).encode("utf-8"))
    return candidates, context


def build_adjudicate_prompt(req: AdjudicationRequest) -> tuple[str, str]:
    cands, ctx = sent_subsets(req)
    user = json.dumps({"request_id": req.request_id, "prompt_version": ADJ_PROMPT_VERSION, "purpose": req.purpose, "cutoff": req.decision_cutoff,
                       "candidates": cands, "context": ctx, "truncated": {"messages": len(req.context) - len(ctx), "candidates": len(req.candidates) - len(cands)}}, ensure_ascii=False, default=str)
    return SYSTEM_ADJ, user


@dataclass
class AdjudicationResult:
    action: str
    selected_candidate_id: str | None
    evidence_message_ids: list[dict[str, Any]]
    rejected_candidate_ids: list[str]
    reason_codes: list[str]
    provider: str
    attempts: int
    extended: bool
    note: str = ""
    response_hash: str = ""


def validate_adjudication(payload: dict[str, Any], req: AdjudicationRequest) -> list[str]:
    errs: list[str] = []
    if not isinstance(payload, dict):
        return ["payload_not_object"]
    action = payload.get("action")
    if action not in ("link", "new_episode", "unresolved"):
        errs.append(f"bad_action:{action!r}")
    sent_cands, sent_ctx = sent_subsets(req)
    cand_ids = {c["candidate_id"] for c in sent_cands}
    sel = payload.get("selected_candidate_id")
    if action == "link":
        if sel not in cand_ids:
            errs.append(f"selected_not_in_candidates:{sel!r}")
    elif sel is not None:
        errs.append("selected_without_link")
    ctx_ids = {(c["peer_id"], c["message_id"], c["source_version_id"]) for c in sent_ctx}
    ev = payload.get("evidence_message_ids")
    if not isinstance(ev, list):
        errs.append("evidence_not_list")
        ev = []
    for e in ev:
        try:
            k = (e["peer_id"], e["message_id"], e["source_version_id"])
        except (KeyError, TypeError):
            errs.append(f"bad_evidence:{e!r}")
            continue
        if k not in ctx_ids:
            errs.append(f"evidence_not_in_context:{k}")
    if action in ("link", "new_episode") and not ev:
        errs.append("evidence_required")
    rej = payload.get("rejected_candidate_ids")
    if not isinstance(rej, list):
        errs.append("rejected_not_list")
    else:
        expected = cand_ids - ({sel} if action == "link" else set())
        if set(rej) != expected:
            errs.append(f"rejected_mismatch:{sorted(expected - set(rej))}")
    return errs


def adjudicate(req: AdjudicationRequest, *, client: LLMClient | None) -> AdjudicationResult:
    """规则之外的歧义裁决：一次扩展、两次传输重试；证据校验失败或超预算 → unresolved（拒答出口）。"""
    c = gate(client)
    extended = False
    attempts = 0
    for _round in range(2):
        cur = AdjudicationRequest(**{**req.__dict__, "extended": extended})
        if req.decision_cutoff:
            # 只发送能证明在截止前可知的上下文；缺 available_at 的条目不能证明 → 剔除（S13）
            cur.context = [c for c in cur.context if c.get("available_at") is not None and str(c["available_at"]) <= str(req.decision_cutoff)]
        system, user = build_adjudicate_prompt(cur)
        if isinstance(c, RecordedClient) and not c.has(system=system, user=user, schema_name=SCHEMA_NAME_ADJ):
            return AdjudicationResult("unresolved", None, [], [x["candidate_id"] for x in req.candidates], ["ENTRY_LINK_AMBIGUOUS"], c.name, attempts, extended, "no_recording")
        out, n = call_with_retry(c, system=system, user=user, schema_name=SCHEMA_NAME_ADJ)
        attempts += n
        if isinstance(out, Abstention):
            return AdjudicationResult("unresolved", None, [], [x["candidate_id"] for x in req.candidates], [out.reason_code], c.name, attempts, extended, out.note)
        errs = validate_adjudication(out.payload, cur)  # 只对照实际发送（截止过滤+预算截断后）的候选与上下文
        if errs:
            return AdjudicationResult("unresolved", None, [], [x["candidate_id"] for x in req.candidates], ["ENTRY_LINK_AMBIGUOUS"], c.name, attempts, extended, "evidence_rejected:" + ";".join(errs)[:200], out.response_hash)
        p = out.payload
        if p["action"] == "unresolved" and p.get("needs_more_context") and not extended:
            extended = True
            continue
        return AdjudicationResult(p["action"], p.get("selected_candidate_id"), list(p.get("evidence_message_ids") or []), list(p.get("rejected_candidate_ids") or []),
                                  [r for r in p.get("reason_codes") or []], c.name, attempts, extended, "", out.response_hash)
    return AdjudicationResult("unresolved", None, [], [x["candidate_id"] for x in req.candidates], ["ENTRY_LINK_AMBIGUOUS"], c.name, attempts, extended, "still_ambiguous_after_extension")


__all__ = [
    "Abstention", "AdjudicationRequest", "AdjudicationResult", "LLMClient", "LLMResponse", "NoNetworkClient", "NoOcr", "OcrProvider", "OcrResult", "RecordedClient", "RecordedOcr",
    "TransportError", "PROMPT_VERSION", "SCHEMA_NAME_EXTRACT", "SCHEMA_NAME_ADJ", "SYSTEM_EXTRACT", "adjudicate", "build_adjudicate_prompt", "build_extract_prompt", "call_with_retry",
    "gate", "record_key", "validate_adjudication", "validate_evidence",
]
