"""层 3 抽取：确定性解析器（全消息分类 + 字段 + span）+ LLM 抽取接口（录制夹具；真实调用 gated）+ bench 报告。

设计（合并稿 C.2 Telegram 3 / ADR-G1 §2.2 B1）：
- 全消息（含纯图、噪音、服务消息除外）都进分类框；kind ∈ 契约 §2 枚举。
- 每个数字必须能定位：spans[{field,start,end,source=text}]，text[start:end] 即该数字 token（含单位）。
- 单位：只转换**有证据**的单位（万/k）；同一条消息内显式单位可向无单位的同量级数字传播（记 spans.field=unit_anchor）。
  无单位的缩写（"747"）不猜倍数，留给层 5 的数量级门（UNIT_SCALE_CONFLICT）。
- parser 与 LLM 各自成行（extractor.name 区分），不互相投票；LLM 行证据 span 校验失败 → 拒收（Abstention 行，kind=undecidable）。
- 输出 silver/extracted_event.parquet（契约 §2 + §9.1 增列）；extract_id = sha256(source_version_id, extractor, version)。
"""
from __future__ import annotations

import argparse
import json
from decimal import Decimal
import math
import os
import pathlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import polars as pl

from .lake import D12, LayerLedger, Layout, append_quarantine, cum_prev, loss_row_from_ledger, mapping_rows, now_utc, preserve_ingested_at, q12, quarantine_row, schema_hash, stable_id, write_loss, write_mapping, write_parquet_atomic
from .llm import SCHEMA_NAME_EXTRACT, Abstention, LLMClient, NoOcr, OcrProvider, RecordedClient, RecordedOcr, build_extract_prompt, call_with_retry, gate, validate_evidence
from .reasons import Reason

RULE_VERSION = "tg3-extract-v0.6"  # W01/W02: LLM numeric evidence Decimal end-to-end
PARSER_VERSION = "parser-v0.5"

KINDS = (
    "entry_proposal", "amend", "cancel", "expire", "entry_claimed", "add", "reduce", "stop_move", "tp_ladder", "close_claimed",
    "correction", "delete_notice", "analysis", "result_post", "chatter", "undecidable",
)

EXTRACTED_EVENT_SCHEMA: dict[str, Any] = {
    "extract_id": pl.String,
    "source_version_id": pl.String,
    "channel_id": pl.Int64,
    "message_id": pl.Int64,
    "version_no": pl.Int32,
    "branch_index": pl.Int32,
    "checks": pl.String,
    "kind": pl.String,
    "symbol_raw": pl.String,
    "instrument_id": pl.String,  # 层 4 规范化前为空（品种当时身份由 validate 决定）
    "side": pl.String,
    "entry": pl.Struct({"lo": D12, "hi": D12, "kind": pl.String}),
    "entries": pl.List(D12),  # ladder 各档（entry.kind=ladder 时）
    "stop": D12,
    "tps": pl.List(pl.Struct({"level": D12, "fraction": D12, "kind": pl.String})),  # A8：契约 §9.1 原文 decimal?
    "size_hint": pl.Struct({"qty": D12, "fraction": D12, "leverage": pl.Int64, "notional": D12}),
    "expires_after_s": pl.Int64,
    "plan_ref": pl.String,
    "spans": pl.List(pl.Struct({"field": pl.String, "start": pl.Int64, "end": pl.Int64, "source": pl.String})),
    "bboxes": pl.List(pl.Struct({"field": pl.String, "media_hash": pl.String, "x0": pl.Float64, "y0": pl.Float64, "x1": pl.Float64, "y1": pl.Float64})),
    "extractor": pl.Struct({"name": pl.String, "version": pl.String, "model": pl.String}),
    "confidence_bucket": pl.String,
    "reason_codes": pl.List(pl.String),
    "entry_mode": pl.String,  # price | market_ref | unknown
    "ocr": pl.String,  # json {provider, version, n_media, n_readable}
    "llm_meta": pl.String,  # json {usage, model, response_hash, attempts, rejected}
    "text_hash": pl.String,
    "rule_version": pl.String,
    "batch_id": pl.String,
    "event_time": pl.Datetime("us", "UTC"),
    "available_at": pl.Datetime("us", "UTC"),
    "ingested_at": pl.Datetime("us", "UTC"),
}
SCHEMA_HASH = schema_hash(EXTRACTED_EVENT_SCHEMA)

# ---------------------------------------------------------------- 数字与单位
NUM_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(?:\s*(万|w|W|k|K))?(?![\d])")
RANGE_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*[-–~～至到]\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(万|w|W|k|K)?(?![\d])")
UNIT_MULT = {"万": 10000, "w": 10000, "W": 10000, "k": 1000, "K": 1000}


def parse_number_token(tok: str) -> Decimal | None:
    m = NUM_RE.fullmatch(tok.strip())
    if not m:
        return None
    v = Decimal(m.group(1).replace(",", ""))
    return round(v * UNIT_MULT.get(m.group(2) or "", 1), 12)


@dataclass
class Num:
    value: Decimal
    start: int
    end: int
    unit: str | None
    raw: Decimal

    @property
    def text_span(self) -> tuple[int, int]:
        return self.start, self.end


def find_numbers(text: str, lo: int = 0, hi: int | None = None) -> list[Num]:
    hi = len(text) if hi is None else hi
    out: list[Num] = []
    for m in NUM_RE.finditer(text, lo, hi):
        raw = Decimal(m.group(1).replace(",", ""))
        unit = m.group(2)
        out.append(Num(round(raw * UNIT_MULT.get(unit or "", 1), 12), m.start(), m.end(), unit, raw))
    return out


# ---------------------------------------------------------------- 词典
SYMBOL_ALIASES: dict[str, str] = {
    "比特币": "BTC", "大饼": "BTC", "饼": "BTC", "以太坊": "ETH", "以太": "ETH", "姨太": "ETH", "索拉": "SOL", "谷歌": "GOOGL",
    "美光": "MU", "原油": "CL", "美油": "CL", "布伦特": "BZ", "黄金": "XAU", "白银": "XAG",
}
SYMBOL_STOP = {
    "TP", "SL", "USD", "USDT", "RSI", "MA", "EMA", "BE", "NFP", "CPI", "AREAN", "DC", "TV", "VAL", "OK", "LONG", "SHORT", "GM",
    "FOMO", "ATH", "ATL", "HTF", "LTF", "PA", "AI", "ETF", "FED", "GDP", "PMI", "VIP", "ID", "UTC", "AM", "PM", "MIGRATION", "TEST",
    "HERMES", "JSON", "API", "P", "H", "D", "W", "M", "IMO", "R", "PNL", "ROI", "SEC", "FUD",
}
SYMBOL_TOKEN_RE = re.compile(r"(?<![A-Za-z])[#$]?([A-Z]{2,6})(?:USDT(?:\.P)?|/USDT)?(?![A-Za-z])")
LOWER_SYMBOL_RE = re.compile(r"[空多]\s*个?\s*(btc|eth|sol|bnb|doge|xrp)(?![A-Za-z])", re.I)

LONG_RE = re.compile(r"做多|多单|买入|购买|做个多|试一个多|进个多|开多|#?LONG\b|\blong\b", re.I)
SHORT_RE = re.compile(r"做空|空单|做个空|试一个空|进个空|开空|补空|空个|#?SHORT\b|\bshort\b", re.I)

NO_TRADE_RE = re.compile(r"不做单|观望|不要玩|不要做|不建议|别追|先别|等.{0,12}再|等.{0,12}我就会发单|MIGRATION TEST|connectivity check|Do not call trading tools", re.I)
CLOSE_RE = re.compile(r"(?<!不)(平仓(?!\s*\d+\s*%)|全部止盈离场|止损离场|止损出局|出局|全平|清仓|全部离场|止盈全部离场|全部止盈|平掉(?!一部分))")
REDUCE_RE = re.compile(r"止盈一半|减仓|仓位减半|减半仓|平掉一部分|部分止盈|止盈\s*\d+\s*%|锁定\s*\d+\s*%?\s*的?利润|平仓\s*\d+\s*%")
STOP_MOVE_RE = re.compile(r"止损(位|点)?\s*(上移|下移|移动|移|调整|调|推|提|设置|设|改)\s*(到|至|为)?|移至|推一个保本|保本止损|止损.{0,6}保本|移到保本|止损位?调整到")
ENTRY_CLAIM_RE = re.compile(r"已(经)?\s*(进场|入场|建仓|上车|开仓|进了|成交)")
CANCEL_RE = re.compile(r"取消(本|这|该)?单|撤单|撤销|(?<!自动)(?<!未成交)作废|计划取消")
EXPIRE_RE = re.compile(r"已过期|已失效|到期作废|过期作废")
CORRECTION_RE = re.compile(r"更正|纠正|写错|有误|勘误")
DELETE_RE = re.compile(r"已删除|删了上一条|删除上一条")
RESULT_RE = re.compile(r"到达|达成|触及|到了|已到|命中|止盈了|止损了|结果如预期|正如预期")
ENTRY_VERB_RE = re.compile(r"入场|建仓|进(个|一个)?(多|空)单|试一个(多|空)单|开(多|空)|布局|挂单|埋伏|加仓|补(多|空)|建立头寸")
SL_KEY_RE = re.compile(r"止损(价|位|点)?|防守|SL\b", re.I)
TP_KEY_RE = re.compile(r"止盈|目标(价|位)?|TP\d?\b|Target", re.I)
ENTRY_KEY_RE = re.compile(r"入场(价|位|区域|区间)?|建仓(价|位)?|买入区域|进场(价|位)?|Entry", re.I)
LEV_RE = re.compile(r"(?<![\d.])(\d{1,3})\s*(倍|[xX]杠杆|x\b)")
FRACTION_RE = re.compile(r"仓位\s*[:：]?\s*(\d{1,3})\s*%|(\d{1,3})\s*%\s*(的)?仓位")
EXPIRY_RE = re.compile(r"(\d{1,3})\s*(小时|h|H)\s*内?有效")
PLAN_REF_RE = re.compile(r"(计划|Plan|第)\s*#?([A-Za-z0-9]{1,4})\s*(号|单)?", re.I)
PCT_AFTER_RE = re.compile(r"^\s*(%|％|倍|x|X|个点|点)")
PRICE_CTX_RE = re.compile(r"阻力|支撑|颈线|附近|位|价|前高|前低|回踩|突破|跌破|涨破|上方|下方|入场|止损|止盈|目标")
NONPRICE_CTX_RE = re.compile(r"成交量|交易量|持仓量|量|人数|粉丝|资金|本金|利润|收益|赚|亏|美金|美元|资产|市值|仓位|杠杆")


@dataclass
class ParseResult:
    branch_index: int = 0
    checks: dict[str, Any] = field(default_factory=dict)
    kind: str = "undecidable"
    symbol_raw: str | None = None
    side: str | None = None
    entry: dict[str, Any] | None = None
    entries: list[Decimal] = field(default_factory=list)
    stop: Decimal | None = None
    tps: list[dict[str, Any]] = field(default_factory=list)
    size_hint: dict[str, Any] | None = None
    expires_after_s: int | None = None
    plan_ref: str | None = None
    spans: list[dict[str, Any]] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    confidence_bucket: str = "low"
    notes: list[str] = field(default_factory=list)
    ocr: dict[str, Any] | None = None
    bboxes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def entry_mode(self) -> str:
        """price | market_ref | unknown（无原文依据不猜市价）。"""
        if self.entry is not None:
            return "price"
        return "market_ref" if "market_ref" in self.notes else "unknown"

    def span(self, fld: str, start: int, end: int) -> None:
        self.spans.append({"field": fld, "start": start, "end": end, "source": "text"})


SENTENCE_END_RE = re.compile(r"[\n；;。！!？?]")
OTHER_SYMBOL_STOP_RE = re.compile(r"(?<![A-Za-z])[#$]?(?:[A-Z]{2,6})(?![A-Za-z])|比特币|大饼|以太坊|以太|索拉|谷歌|美光|原油|美油|布伦特")
MARKET_EVIDENCE_RE = re.compile(r"市价|现价|当前价|当前价格|这里进|这里做|直接进|现在进|直接开|现价进|市价进|刚起|这里空|这里多|进个|进一个|试一个")


def _segment(text: str, key_re: re.Pattern, *, stop_res: tuple[re.Pattern, ...], max_len: int = 80) -> tuple[int, int] | None:
    """关键词之后到下一个关键词 / 句读（换行、分号、句号、叹问号）前的片段（S06：不跨句取数字）。"""
    m = key_re.search(text)
    if not m:
        return None
    start = m.end()
    end = min(len(text), start + max_len)
    se = SENTENCE_END_RE.search(text, start, end)
    if se:
        end = se.start()
    for sm in OTHER_SYMBOL_STOP_RE.finditer(text, start, end):  # 另一品种出现 = 本字段片段结束
        if sm.group(0).lstrip("#$") not in SYMBOL_STOP:
            end = sm.start()
            break
    for sr in stop_res:
        m2 = sr.search(text, start, end)
        if m2:
            end = min(end, m2.start())
    return start, end


def _not_pct(text: str, n: Num) -> bool:
    return not PCT_AFTER_RE.match(text[n.end : n.end + 3])


def detect_symbol(text: str, *, anchor: int | None = None) -> tuple[str | None, tuple[int, int] | None]:
    cands: list[tuple[int, str, int, int]] = []  # (priority, sym, start, end)
    for m in SYMBOL_TOKEN_RE.finditer(text):
        sym = m.group(1)
        if sym in SYMBOL_STOP:
            continue
        pri = 0 if text[m.start()] in "#$" else 1
        cands.append((pri, sym, m.start(), m.end()))
    for m in LOWER_SYMBOL_RE.finditer(text):
        cands.append((1, m.group(1).upper(), m.start(1), m.end(1)))
    for alias, sym in SYMBOL_ALIASES.items():
        i = text.find(alias)
        if i != -1:
            cands.append((2, sym, i, i + len(alias)))
    if not cands:
        return None, None
    if anchor is not None:
        cands.sort(key=lambda c: (abs(c[2] - anchor), c[0], c[2]))
    else:
        cands.sort(key=lambda c: (c[0], c[2]))
    _, sym, a, b = cands[0]
    return sym, (a, b)


SIDE_VERB_LONG_RE = re.compile(r"试一个多|进个多|进一个多|开多|做个多|做多|买入区域|购买策略|多单(?=.{0,3}(计划|策略|建仓|入场))")
SIDE_VERB_SHORT_RE = re.compile(r"试一个空|进个空|进一个空|开空|做个空|做空(?!操作)|空个|补空|空单(?=.{0,3}(计划|策略|建仓|入场))")


def detect_side(text: str) -> tuple[str | None, tuple[int, int] | None, bool]:
    lm, sm = LONG_RE.search(text), SHORT_RE.search(text)
    vl, vs = SIDE_VERB_LONG_RE.search(text), SIDE_VERB_SHORT_RE.search(text)
    if lm and sm and (vl or vs) and not (vl and vs):
        m = vl or vs
        return ("long" if vl else "short"), (m.start(), m.end()), False
    # "方向：做多" 优先
    dm = re.search(r"方向\s*[:：]\s*(做多|做空|多|空|LONG|SHORT)", text, re.I)
    if dm:
        w = dm.group(1).upper()
        side = "long" if w in ("做多", "多", "LONG") else "short"
        return side, (dm.start(1), dm.end(1)), False
    if lm and sm:
        # 两者都有：按出现先后，标歧义
        first = lm if lm.start() < sm.start() else sm
        return ("long" if first is lm else "short"), (first.start(), first.end()), True
    if lm:
        return "long", (lm.start(), lm.end()), False
    if sm:
        return "short", (sm.start(), sm.end()), False
    return None, None, False


def _propagate_units(text: str, res: ParseResult, nums_all: list[Num]) -> None:
    """同一消息内显式 万/k 单位向同量级无单位数字传播（证据 = 单位 token 所在 span）。"""
    price_spans = {(sp["start"], sp["end"]) for sp in res.spans if sp["field"] in ("entry.lo", "entry.hi", "entries", "stop", "tps")}

    def price_context(n: Num) -> bool:
        """锚必须是本品种的价格：先排除紧邻另一品种的数字（别的币的价位，S06 R06c），再看是否在价格字段 span 内或价位语境内且非量/资金/收益类词。"""
        before = text[max(0, n.start - 10): n.start]
        other, _ = detect_symbol(before)
        if other and res.symbol_raw and other != res.symbol_raw:
            return False
        if any(a <= n.start and n.end <= b for a, b in price_spans):
            return True
        ctx = text[max(0, n.start - 8): n.end + 8]
        if NONPRICE_CTX_RE.search(text[max(0, n.start - 8): n.start]):
            return False
        return bool(PRICE_CTX_RE.search(ctx))

    # 只用价格语境里带单位的数字作锚（"历史成交量 6.5万" 这类非价格数字不得放大入场/止损）
    anchors = [n for n in nums_all if n.unit and price_context(n)]
    if not anchors:
        return
    mult = UNIT_MULT[anchors[0].unit]
    anchor_val = anchors[0].value
    res.span("unit_anchor", anchors[0].start, anchors[0].end)

    def fix(v: Decimal | None) -> Decimal | None:
        if v is None or v <= 0:
            return v
        if 0.3 <= (v * mult) / anchor_val <= 3.0 and not (0.3 <= v / anchor_val <= 3.0):
            res.notes.append(f"unit_propagated:{v}->{round(v * mult, 12)}")
            return round(v * mult, 12)
        return v

    if res.entry:
        res.entry["lo"], res.entry["hi"] = fix(res.entry["lo"]), fix(res.entry["hi"])
    res.entries = [fix(v) for v in res.entries]
    res.stop = fix(res.stop)
    for t in res.tps:
        t["level"] = fix(t["level"])


def parse_message(text: str) -> ParseResult:
    """确定性解析器：分类 + 字段 + span。纯函数。"""
    # Quoted historical text is evidence for linking, not a fresh proposal or price patch.
    masked = re.sub(r'[“"「][^”"」]+[”"」]', lambda m: " " * len(m.group(0)), text)
    masked = re.sub(r"(?m)^>[^\n]*", lambda m: " " * len(m.group(0)), masked)
    if masked != text and masked.strip():
        return parse_message(masked)
    res = ParseResult()
    if not text.strip():
        res.kind = "undecidable"
        res.reason_codes.append(Reason.OCR_UNREADABLE)
        res.notes.append("empty_text:ocr_not_run")
        return res
    shorthand = re.match(r"^[;；\s]*(?:(?:独立单|新单|第[一二三123]单)[:： ]*)?(BTC|ETH|SOL|FIL|ZRO|TIA|ETHFI)\s*(做多|做空|多|空)\s*[:：@]?\s*(\d+(?:\.\d+)?(?:[kK万])?)", text)
    nums_all = find_numbers(text)
    side, side_span, side_ambig = detect_side(text)
    if shorthand:
        side = "long" if shorthand[2] in ("多", "做多") else "short"
        side_span = shorthand.span(2)
        value = parse_number_token(shorthand[3])
        res.entry = {"lo": value, "hi": value, "kind": "limit"}
        res.span("entry.lo", *shorthand.span(3))
    if side_span:
        res.span("side", *side_span)
    res.side = side
    if side_ambig:
        res.reason_codes.append(Reason.INTENT_AMBIGUOUS)
        res.notes.append("both_sides_mentioned")

    # ---- 字段：SL
    used: set[tuple[int, int]] = set()
    for km in SL_KEY_RE.finditer(text):
        seg = _segment(text[km.start():], re.compile(re.escape(km.group(0))), stop_res=(TP_KEY_RE,), max_len=60)
        if not seg:
            continue
        lo, hi = km.start() + seg[0], km.start() + seg[1]
        hit = None
        for n in find_numbers(text, lo, hi):
            if _not_pct(text, n) and n.raw >= 0.0001:
                hit = n
                break
        if hit is not None:
            res.stop = hit.value
            res.span("stop", hit.start, hit.end)
            used.add(hit.text_span)
            break
    if res.stop is None:
        m = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(止损|防守)", text)
        if m:
            n = find_numbers(text, m.start(1), m.end(1))[0]
            res.stop = n.value
            res.span("stop", n.start, n.end)
            used.add(n.text_span)
    # ---- 字段：TP
    tp_seg = _segment(text, TP_KEY_RE, stop_res=(SL_KEY_RE, ENTRY_KEY_RE, re.compile(r"理由|注：")), max_len=120)
    if tp_seg:
        for n in find_numbers(text, *tp_seg):
            after = text[n.end : n.end + 1]
            if after in ("：", ":") and n.raw <= 9:  # 点位1：/ 1： 索引
                continue
            if not _not_pct(text, n):
                continue
            if n.text_span in used:
                continue
            res.tps.append({"level": n.value, "fraction": None, "kind": "price"})
            res.span("tps", n.start, n.end)
            used.add(n.text_span)
    # ---- 字段：入场
    en_seg = _segment(text, ENTRY_KEY_RE, stop_res=(SL_KEY_RE, TP_KEY_RE, re.compile(r"理由|信心度|倍数|仓位")), max_len=80)
    entry_nums: list[Num] = []
    if en_seg:
        rm = RANGE_RE.search(text, *en_seg)
        if rm:
            unit = rm.group(3)
            mult = UNIT_MULT.get(unit or "", 1)
            lo, hi = round(Decimal(rm.group(1).replace(",", "")) * mult, 12), round(Decimal(rm.group(2).replace(",", "")) * mult, 12)
            res.entry = {"lo": min(lo, hi), "hi": max(lo, hi), "kind": "zone"}
            res.span("entry.lo", rm.start(1), rm.end(1))
            res.span("entry.hi", rm.start(2), rm.end())
            used.add((rm.start(), rm.end()))
        else:
            entry_nums = [n for n in find_numbers(text, *en_seg) if _not_pct(text, n) and n.text_span not in used]
            if MARKET_EVIDENCE_RE.search(text[en_seg[0] : en_seg[1]]):
                res.notes.append("market_ref")
                res.span("entry.market_ref", en_seg[0], en_seg[1])
    # Titan 式多档：首次/第二次入场价（或建仓）
    ladder = []
    for m in re.finditer(r"(首次|第一次|第二次|第三次|再次)\s*(入场价?|建仓|买入|做空)\s*[:：]?\s*>?\s*\(?\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)", text):
        n = find_numbers(text, m.start(3), m.end(3))[0]
        ladder.append(n)
    if len(ladder) >= 1 and res.entry is None:
        entry_nums = ladder
    if res.entry is None and entry_nums:
        if len(entry_nums) >= 2:
            res.entry = {"lo": min(n.value for n in entry_nums), "hi": max(n.value for n in entry_nums), "kind": "ladder"}
            res.entries = [n.value for n in entry_nums]
            for n in entry_nums:
                res.span("entries", n.start, n.end)
        else:
            n = entry_nums[0]
            kind = "ladder" if "market_ref" in res.notes else "limit"
            res.entry = {"lo": n.value, "hi": n.value, "kind": kind}
            if kind == "ladder":
                res.entries = [n.value]
                res.span("entries", n.start, n.end)
            else:
                res.span("entry.lo", n.start, n.end)
        for n in entry_nums:
            used.add(n.text_span)
    # 补空/补多 追加档
    if res.entry and res.entry["kind"] in ("ladder", "limit", "zone"):
        for m in re.finditer(r"((?:(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*[、,，和及]\s*)*(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?))\s*(附近)?\s*(补空|补多|加仓|补仓|补一笔)", text):
            for n in find_numbers(text, m.start(1), m.end(1)):
                if n.text_span in used or not _not_pct(text, n):
                    continue
                if res.entry["kind"] != "ladder":
                    res.entries = [res.entry["lo"]] if res.entry["lo"] == res.entry["hi"] else [res.entry["lo"], res.entry["hi"]]
                    res.entry["kind"] = "ladder"
                res.entries.append(n.value)
                res.entry["lo"], res.entry["hi"] = min(res.entries), max(res.entries)
                res.span("entries", n.start, n.end)
                used.add(n.text_span)
    # 坚果式："测试747 ... 试一个多单"：入场数字在 side 关键词前后 30 字内、且未被占用
    if res.entry is None and side_span and ENTRY_VERB_RE.search(text, max(0, side_span[0] - 12), min(len(text), side_span[1] + 12)):
        near = [n for n in nums_all if abs(n.start - side_span[0]) <= 40 and n.text_span not in used and _not_pct(text, n) and n.raw >= 1]
        if near:
            n = near[0]
            res.entry = {"lo": n.value, "hi": n.value, "kind": "limit"}
            res.span("entry.lo", n.start, n.end)
            used.add(n.text_span)
    if res.entry is None and side_span:
        mm = MARKET_EVIDENCE_RE.search(text, max(0, side_span[0] - 12), min(len(text), side_span[1] + 12))
        if mm:
            res.notes.append("market_ref")
            res.span("entry.market_ref", mm.start(), mm.end())
    # ---- 品种先行（单位锚需要知道本消息品种）
    # 提议类（有方向且有入场价/入场动词）：取最早出现的品种（信号头）；管理类：取离止损数字最近的品种（"把 ETH 的止损上移到…"）
    _anchor = next((sp["start"] for sp in res.spans if sp["field"] == "stop"), None)
    _is_proposal_like = bool(side_span) and (res.entry is not None or ENTRY_VERB_RE.search(text, max(0, side_span[0] - 12), min(len(text), side_span[1] + 12)) is not None)
    if _is_proposal_like:
        _sym, _sym_span = detect_symbol(text)
    else:
        _sym, _sym_span = detect_symbol(text, anchor=_anchor)
        if _sym is None and _anchor is not None:
            _sym, _sym_span = detect_symbol(text)
    res.symbol_raw = _sym
    # ---- 单位传播 / 杠杆 / 仓位 / 期限 / 计划号
    _propagate_units(text, res, nums_all)
    lev = LEV_RE.search(text)
    fr = FRACTION_RE.search(text)
    if lev or fr:
        res.size_hint = {"qty": None, "fraction": None, "leverage": None, "notional": None}
        if lev:
            res.size_hint["leverage"] = int(lev.group(1))
            res.span("size_hint.leverage", lev.start(1), lev.end(1))
        if fr:
            g = fr.group(1) or fr.group(2)
            res.size_hint["fraction"] = Decimal(g) / 100
            res.span("size_hint.fraction", fr.start(), fr.end())
    for key, pattern in (("qty", r"(?:数量|quantity|qty)\s*[:：]?\s*(\d+(?:\.\d+)?)"), ("notional", r"(?:名义金额|名义|notional)\s*[:：]?\s*(\d+(?:\.\d+)?)")):
        match = re.search(pattern, text, re.I)
        if match:
            if res.size_hint is None:
                res.size_hint = {"qty": None, "fraction": None, "leverage": None, "notional": None}
            res.size_hint[key] = Decimal(match.group(1))
            res.span(f"size_hint.{key}", match.start(1), match.end(1))
    ex = EXPIRY_RE.search(text)
    if ex:
        res.expires_after_s = int(ex.group(1)) * 3600
        res.span("expiry", ex.start(), ex.end())
    pr = PLAN_REF_RE.search(text)
    if pr and pr.group(1) != "第":
        res.plan_ref = pr.group(2)
        res.span("plan_ref", pr.start(), pr.end())
    # ---- 品种（已在单位传播前确定）
    sym, sym_span = _sym, _sym_span
    if sym_span:
        res.span("symbol_raw", *sym_span)

    # ---- 分类
    has_entry = res.entry is not None
    if re.search(r"撤回上一条(?:断言)?|上一条作废", text):
        res.kind = "correction"
    elif NO_TRADE_RE.search(text) and not has_entry:
        res.kind = "analysis" if (sym or side) else "chatter"
    elif CORRECTION_RE.search(text) and (has_entry or res.stop is not None):
        res.kind = "correction"
    elif DELETE_RE.search(text):
        res.kind = "delete_notice"
    elif CANCEL_RE.search(text):
        res.kind = "cancel"
    elif EXPIRE_RE.search(text):
        res.kind = "expire"
    elif STOP_MOVE_RE.search(text) and res.stop is not None and not has_entry:
        res.kind = "stop_move"
    elif re.search(r"加仓|加码", text) and not has_entry:
        res.kind = "add"
    elif CLOSE_RE.search(text) and not has_entry:
        res.kind = "close_claimed"
    elif REDUCE_RE.search(text) and not has_entry:
        res.kind = "reduce"
    elif STOP_MOVE_RE.search(text) and not has_entry:
        res.kind = "stop_move"
        res.reason_codes.append(Reason.INTENT_AMBIGUOUS)
        res.notes.append("stop_move_without_price")
    elif ENTRY_CLAIM_RE.search(text) and not has_entry:
        res.kind = "entry_claimed"
    elif side and (has_entry or ENTRY_VERB_RE.search(text) or res.stop is not None):
        res.kind = "entry_proposal"
    elif has_entry or (sym and ENTRY_VERB_RE.search(text)):
        res.kind = "entry_proposal"
        res.reason_codes.append(Reason.INTENT_AMBIGUOUS)
        res.notes.append("no_side")
    elif RESULT_RE.search(text) and (sym or TP_KEY_RE.search(text)):
        res.kind = "result_post"
    elif sym or side or len(nums_all) >= 2:
        res.kind = "analysis"
    else:
        res.kind = "chatter"
    if res.kind in ("entry_proposal", "stop_move", "reduce", "close_claimed", "entry_claimed", "add") and sym is None:
        if Reason.INTENT_AMBIGUOUS not in res.reason_codes:
            res.reason_codes.append(Reason.INTENT_AMBIGUOUS)
        res.notes.append("no_symbol")
    if res.kind in ("chatter", "analysis", "result_post"):
        res.reason_codes.append(Reason.NOT_SIGNAL)
    # ---- 置信层
    if res.kind == "entry_proposal" and sym and side and has_entry and res.stop is not None and not res.reason_codes:
        res.confidence_bucket = "high"
    elif res.kind in ("entry_proposal", "stop_move", "reduce", "close_claimed") and sym and not res.reason_codes:
        res.confidence_bucket = "mid"
    elif res.kind in ("chatter", "analysis") and not sym and not side:
        res.confidence_bucket = "high"
    else:
        res.confidence_bucket = "low"
    return res


def parse_branches(text: str) -> list[ParseResult]:
    """Split only explicit independent proposals; multiple prices in one proposal remain a ladder."""
    starts = list(re.finditer(r"(?:^|[;；\n])\s*(?:(?:独立单|新单|第[一二三123]单|计划[A-Za-z0-9]+)[:： ]*)?(?:BTC|ETH|SOL|FIL|ZRO|TIA|ETHFI)\s*(?:做多|做空|多|空|long|short)", text, re.I))
    if len(starts) < 2:
        return [parse_message(text)]
    results = []
    for i, match in enumerate(starts):
        start = match.start()
        end = len(text)
        if i + 1 < len(starts):
            end = starts[i + 1].start()
        part = parse_message(text[start:end])
        if part.kind != "entry_proposal":
            return [parse_message(text)]
        part.branch_index = i
        for span in part.spans:
            span["start"] += start
            span["end"] += start
        results.append(part)
    return results


# ---------------------------------------------------------------- OCR（S13）
def apply_ocr(res: ParseResult, text: str, media_hashes: list[str], ocr: OcrProvider) -> ParseResult:
    """图片证据：纯图 → 用 OCR 文本解析（span.source=ocr，数字带 bbox）；图文并存 → 独立比对数字，冲突 TEXT_IMAGE_CONFLICT。"""
    if not media_hashes:
        return res
    results = [(h, ocr.read(h)) for h in media_hashes]
    ran = [(h, r) for h, r in results if r.status != "not_run"]
    if not ran:
        res.notes.append("ocr_not_run")
        return res
    readable = [(h, r) for h, r in ran if r.status == "ok"]
    res.ocr = {"provider": ran[0][1].provider, "version": ran[0][1].version, "n_media": len(media_hashes), "n_readable": len(readable)}
    if not readable:
        if not text.strip():
            res.reason_codes = [c for c in res.reason_codes if c != Reason.OCR_UNREADABLE] + [Reason.OCR_UNREADABLE]
            res.notes = [n for n in res.notes if n != "empty_text:ocr_not_run"] + ["ocr_unreadable"]
        return res
    from .llm import valid_ocr_number
    ocr_nums: dict[float, tuple[str, list[float]]] = {}
    for h, r in readable:
        for n in r.numbers:
            if not valid_ocr_number(n, None) or not n.get("bbox"):
                continue
            ocr_nums[Decimal(str(n["value"]))] = (h, [float(x) for x in (n.get("bbox") or [])])
    if not text.strip():
        # 纯图：解析 OCR 文本，span 标 ocr 来源，数字挂 bbox
        h, r = readable[0]
        sub = parse_message(r.text)
        for sp in sub.spans:
            sp["source"] = "ocr"
        sub.notes.append("parsed_from_ocr")
        sub.ocr = res.ocr
        for sp in sub.spans:
            v = parse_number_token(r.text[sp["start"]:sp["end"]])
            if v is not None and v in ocr_nums and len(ocr_nums[v][1]) == 4:
                sub.bboxes.append({"field": sp["field"], "media_hash": ocr_nums[v][0], "x0": ocr_nums[v][1][0], "y0": ocr_nums[v][1][1], "x1": ocr_nums[v][1][2], "y1": ocr_nums[v][1][3]})
        numeric_values = [n.value for n in find_numbers(r.text)]
        if not numeric_values or any(v not in ocr_nums or ocr_nums[v][0] != h for v in numeric_values):
            sub.kind = "undecidable"
            sub.entry, sub.stop, sub.size_hint = None, None, None
            sub.entries, sub.tps, sub.bboxes = [], [], []
            sub.reason_codes = [Reason.OCR_UNREADABLE]
            sub.notes.append("numeric_field_without_image_bbox")
        else:
            sub.reason_codes = [c for c in sub.reason_codes if c != Reason.OCR_UNREADABLE]
        return sub
    # Match semantic fields, never a magnitude neighbourhood: 100 vs 1000 is a conflict too.
    def values(parsed):
        out = {}
        if parsed.entry:
            out["entry.lo"] = parsed.entry["lo"]
            out["entry.hi"] = parsed.entry["hi"]
        if parsed.stop is not None:
            out["stop"] = parsed.stop
        for i, tp in enumerate(parsed.tps):
            out[f"tps[{i}].level"] = tp["level"]
        out["symbol_raw"] = parsed.symbol_raw
        out["side"] = parsed.side
        out["expires_after_s"] = parsed.expires_after_s
        for key, value in (parsed.size_hint or {}).items():
            out[f"size_hint.{key}"] = value
        for i, tp in enumerate(parsed.tps):
            out[f"tps[{i}].fraction"] = tp.get("fraction")
        return out

    text_vals = values(res)
    for media_hash, recording in readable:
        image_parsed = parse_message(recording.text)
        image_vals = values(image_parsed)
        for field_name, value in text_vals.items():
            if field_name not in image_vals:
                continue
            other = image_vals[field_name]
            agrees = value == other
            if isinstance(value, (int, float, Decimal)) and isinstance(other, (int, float, Decimal)):
                agrees = q12(value) == q12(other)
            if field_name not in ("symbol_raw", "side"):
                agrees = agrees and text_vals["symbol_raw"] == image_vals["symbol_raw"] and text_vals["side"] == image_vals["side"]
            evidence_valid = True
            if isinstance(other, (int, float, Decimal)):
                # Compare the raw digits proved by the field span, before percent/time/unit conversion.
                span_field = field_name
                if field_name == "expires_after_s":
                    span_field = "expiry"
                spans = [sp for sp in image_parsed.spans if sp["field"] == span_field]
                if field_name in ("entry.lo", "entry.hi") and not spans:
                    spans = [sp for sp in image_parsed.spans if sp["field"] in ("entry.lo", "entries")]
                tp_match = re.fullmatch(r"tps\[(\d+)\]\.level", field_name)
                if tp_match:
                    all_tp_spans = [sp for sp in image_parsed.spans if sp["field"] == "tps"]
                    index = int(tp_match.group(1))
                    spans = all_tp_spans[index:index + 1]
                raw_values = [number.raw for span in spans for number in find_numbers(recording.text, span["start"], span["end"])]
                evidence_valid = bool(raw_values) and all(any(Decimal(str(n["value"])) == raw and n.get("bbox") for n in recording.numbers) for raw in raw_values)
                if not evidence_valid:
                    agrees = False
                    if Reason.OCR_UNREADABLE not in res.reason_codes:
                        res.reason_codes.append(Reason.OCR_UNREADABLE)
            res.checks.setdefault("text_image", []).append({"field": field_name, "text": str(value), "ocr": str(other), "media_hash": media_hash, "match": agrees, "evidence_valid": evidence_valid})
            if not agrees:
                if Reason.TEXT_IMAGE_CONFLICT not in res.reason_codes:
                    res.reason_codes.append(Reason.TEXT_IMAGE_CONFLICT)
                res.notes.append(f"text_image_conflict:{field_name}")
    for sp in res.spans:
        v = parse_number_token(text[sp["start"]:sp["end"]]) if sp["field"] in ("entry.lo", "entry.hi", "stop", "tps", "entries") else None
        if v is not None and v in ocr_nums and len(ocr_nums[v][1]) == 4:
            res.bboxes.append({"field": sp["field"], "media_hash": ocr_nums[v][0], "x0": ocr_nums[v][1][0], "y0": ocr_nums[v][1][1], "x1": ocr_nums[v][1][2], "y1": ocr_nums[v][1][3]})
    return res


# ---------------------------------------------------------------- LLM 行
def llm_extract(text: str, *, client: LLMClient, channel_name: str, message_date: str | None) -> tuple[ParseResult | None, Abstention | None, dict[str, Any]]:
    system, user = build_extract_prompt(text, channel_name=channel_name, message_date=message_date)
    client = gate(client)
    if isinstance(client, RecordedClient) and not client.has(system=system, user=user, schema_name=SCHEMA_NAME_EXTRACT):
        return None, None, {"skipped": "no_recording"}
    out, attempts = call_with_retry(client, system=system, user=user, schema_name=SCHEMA_NAME_EXTRACT)
    if isinstance(out, Abstention):
        return None, out, {"usage": {}, "attempts": attempts}
    errs = validate_evidence(out.payload, text)
    if errs:
        return None, Abstention(Reason.INTENT_AMBIGUOUS, "evidence_rejected:" + ";".join(errs)[:200]), {"usage": out.usage, "rejected": errs}
    p = out.payload
    e = p.get("entry")
    res = ParseResult(
        kind=p.get("kind") if p.get("kind") in KINDS else "undecidable",
        symbol_raw=p.get("symbol_raw") if isinstance(p.get("symbol_raw"), str) else None,
        side=p.get("side") if p.get("side") in ("long", "short") else None,
        entry={"lo": Decimal(str(e["lo"])), "hi": Decimal(str(e["hi"])), "kind": str(e.get("kind") or "limit")} if isinstance(e, dict) and e.get("lo") is not None and e.get("hi") is not None else None,
        entries=[Decimal(str(x)) for x in p.get("entries") or []],
        stop=Decimal(str(p["stop"])) if p.get("stop") is not None else None,
        tps=[{"level": Decimal(str(t["level"] if isinstance(t, dict) else t)), "fraction": None, "kind": "price"} for t in p.get("tps") or []],
        spans=[{"field": s["field"], "start": int(s["start"]), "end": int(s["end"]), "source": "text"} for s in p.get("spans") or []],
        reason_codes=[r for r in p.get("reason_codes") or [] if r in set(Reason)],
        confidence_bucket="mid",
    )
    return res, None, {"usage": out.usage, "model": out.model, "response_hash": out.response_hash, "attempts": attempts}


# ---------------------------------------------------------------- 落盘
def _row(base: dict[str, Any], res: ParseResult, extractor: dict[str, Any], ingested_at: datetime, *, inherited: list[str] | None = None, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    entry = None
    if res.entry:
        entry = {"lo": res.entry.get("lo"), "hi": res.entry.get("hi"), "kind": res.entry.get("kind")}
    codes = sorted(set(res.reason_codes) | set(inherited or []))
    return {
        "extract_id": stable_id("ex", base["source_version_id"], extractor["name"], extractor["version"], extractor.get("model"), res.branch_index),
        "source_version_id": base["source_version_id"],
        "channel_id": base["channel_id"],
        "message_id": base["message_id"],
        "version_no": base["version_no"],
        "branch_index": res.branch_index,
        "checks": json.dumps(res.checks, sort_keys=True),
        "kind": res.kind,
        "symbol_raw": res.symbol_raw,
        "instrument_id": None,
        "side": res.side,
        "entry": entry,
        "entries": res.entries,
        "stop": res.stop,
        "tps": res.tps,
        "size_hint": res.size_hint,
        "expires_after_s": res.expires_after_s,
        "plan_ref": res.plan_ref,
        "spans": res.spans,
        "bboxes": res.bboxes,
        "extractor": {"name": extractor["name"], "version": extractor["version"], "model": extractor.get("model")},
        "confidence_bucket": res.confidence_bucket,
        "reason_codes": codes,
        "entry_mode": res.entry_mode,
        "ocr": json.dumps(res.ocr, ensure_ascii=False) if res.ocr else None,
        "llm_meta": json.dumps({k: v for k, v in (meta or {}).items() if k in ("usage", "model", "response_hash", "attempts", "rejected")}, ensure_ascii=False, default=str) if meta else None,
        "text_hash": base["content_hash"],
        "rule_version": RULE_VERSION,
        "batch_id": base["batch_id"],
        "event_time": base["event_time"],
        "available_at": base["available_at"],
        "ingested_at": ingested_at,
    }


def extract_frame(mv: pl.DataFrame, dg: pl.DataFrame | None, *, client: LLMClient | None = None, ocr: OcrProvider | None = None, ingested_at: datetime | None = None) -> tuple[pl.DataFrame, list[dict], dict[tuple[int, str], LayerLedger], dict[str, Any]]:
    ingested_at = ingested_at or now_utc()
    ocr = ocr or NoOcr()
    frame = mv.with_columns(pl.col("source_id").struct.field("message_id").alias("message_id"))
    if dg is not None and dg.height:
        frame = frame.join(dg.select("source_version_id", "is_canonical", "canonical_version_id"), on="source_version_id", how="left").with_columns(pl.col("is_canonical").fill_null(True))
    else:
        frame = frame.with_columns(pl.lit(True).alias("is_canonical"), pl.col("source_version_id").alias("canonical_version_id"))
    batch_id = mv["batch_id"][0] if mv.height else "tg-empty"
    rows: list[dict[str, Any]] = []
    qrows: list[dict[str, Any]] = []
    llm_stats = {"rows": 0, "abstain": 0, "rejected": 0, "skipped": 0}
    parser_ex = {"name": "parser", "version": PARSER_VERSION, "model": None}
    ledgers: dict[tuple[int, str], LayerLedger] = {}
    for r in frame.filter(pl.col("is_canonical")).iter_rows(named=True):
        inherited = [c for c in (r["reason_codes"] or []) if c != Reason.NOT_SIGNAL]  # 层 1 隔离原因跨层携带（S07）
        year = str(r["message_date"].year) if r["message_date"] else "unknown"
        led = ledgers.setdefault((r["channel_id"], year), LayerLedger(3, {"channel_id": r["channel_id"], "year": year}, "canonical_version", "extracted_event"))
        if r["message_type"] != "message":
            led.mark(r["source_version_id"], "ok", [Reason.NOT_SIGNAL])
            led.map(r["source_version_id"], None, "excluded", [Reason.NOT_SIGNAL])
            continue
        state = "ok"
        all_reasons = set(inherited)
        for res in parse_branches(r["text"]):
            res = apply_ocr(res, r["text"], list(r["media_hashes"] or []), ocr)
            prow = _row(r, res, parser_ex, ingested_at, inherited=inherited)
            rows.append(prow)
            all_reasons.update(prow["reason_codes"])
            if any(c in prow["reason_codes"] for c in ("INTENT_AMBIGUOUS", "OCR_UNREADABLE", "TEXT_IMAGE_CONFLICT")):
                state = "review"
            relation = "one_to_one"
            if res.branch_index:
                relation = "split"
            led.map(r["source_version_id"], prow["extract_id"], relation, prow["reason_codes"])
        if any(c in inherited for c in ("TIME_UNIT_INVALID", "VERSION_TIME_UNKNOWN", "RAW_HASH_MISMATCH", "SCHEMA_DRIFT")):
            state = "quarantine"
        led.mark(r["source_version_id"], state, sorted(all_reasons))
        if client is not None:
            pr, ab, meta = llm_extract(r["text"], client=client, channel_name=r["channel_name"], message_date=r["message_date"].isoformat() if r["message_date"] else None)
            llm_ex = {"name": "llm", "version": getattr(client, "version", "?"), "model": getattr(client, "model", getattr(client, "name", "?"))}
            if pr is not None:
                lrow = _row(r, pr, llm_ex, ingested_at, inherited=inherited, meta=meta)
                rows.append(lrow)
                led.map(r["source_version_id"], lrow["extract_id"], "split", [])
                llm_stats["rows"] += 1
            elif ab is not None:
                a = ParseResult(kind="undecidable", reason_codes=[ab.reason_code], notes=[ab.note])
                lrow = _row(r, a, llm_ex, ingested_at, inherited=inherited, meta=meta)
                rows.append(lrow)
                led.map(r["source_version_id"], lrow["extract_id"], "split", [ab.reason_code])
                llm_stats["abstain"] += 1
                if ab.note.startswith("evidence_rejected"):
                    llm_stats["rejected"] += 1
            else:
                llm_stats["skipped"] += 1
    from decimal import Decimal

    def _dec(v):
        return None if v is None else Decimal(q12(v))

    for o in rows:
        if o["entry"]:
            o["entry"] = {**o["entry"], "lo": _dec(o["entry"]["lo"]), "hi": _dec(o["entry"]["hi"])}
        o["entries"] = [_dec(v) for v in o["entries"]]
        o["stop"] = _dec(o["stop"])
        o["tps"] = [{"level": _dec(t["level"]), "fraction": _dec(t.get("fraction")), "kind": t.get("kind", "price")} for t in o["tps"]]
        if o["size_hint"]:
            sh = dict(o["size_hint"])
            o["size_hint"] = {"qty": _dec(sh.get("qty")), "fraction": _dec(sh.get("fraction")), "leverage": sh.get("leverage"), "notional": _dec(sh.get("notional"))}
    df = pl.DataFrame(rows, schema=EXTRACTED_EVENT_SCHEMA) if rows else pl.DataFrame(schema=EXTRACTED_EVENT_SCHEMA)
    for o in rows:
        codes = [c for c in o["reason_codes"] if c not in (Reason.NOT_SIGNAL,)]
        if not codes or o["extractor"]["name"] != "parser":
            continue
        qrows.append(quarantine_row(batch_id=batch_id, object_kind="extracted_event", object_id=o["extract_id"], object_version=o["source_version_id"],
                                    partition_id=f"channel={o['channel_id']}", reason_codes=codes, rule_version=RULE_VERSION, schema_hash_=SCHEMA_HASH,
                                    source_refs={"message_id": o["message_id"], "channel_id": o["channel_id"]}, event_time=o["event_time"], available_at=o["available_at"],
                                    ingested_at=ingested_at, field_path="kind", observed_value_ref=o["kind"], expected_contract="research-schema §2 silver/extracted_event"))
    summary = {
        "batch_id": batch_id, "n_canonical": int(frame["is_canonical"].sum()), "n_rows": df.height,
        "kind_dist": {k: v for k, v in sorted(df.filter(pl.col("extractor").struct.field("name") == "parser").group_by("kind").len().iter_rows())} if df.height else {},
        "llm": llm_stats, "ocr_provider": ocr.name, "rule_version": RULE_VERSION,
    }
    if client is not None and df.height:
        summary["co_error"] = co_error_report(df)
    return df, qrows, ledgers, summary


def co_error_report(df: pl.DataFrame) -> dict[str, Any]:
    """字段级 parser/LLM 一致率（同一 source_version 两行都存在时）。无金标时只报分歧率，不报正确率。"""
    p = df.filter(pl.col("extractor").struct.field("name") == "parser")
    l = df.filter(pl.col("extractor").struct.field("name") == "llm").filter(pl.col("kind") != "undecidable")
    j = p.join(l, on="source_version_id", suffix="_llm")
    out: dict[str, Any] = {"n_pairs": j.height}
    if not j.height:
        return out
    for f in ("kind", "symbol_raw", "side", "stop"):
        both = j.filter(pl.col(f).is_not_null() | pl.col(f + "_llm").is_not_null())  # 两边都空不算一致（S13）
        out[f"agree_{f}"] = round(float((both[f] == both[f + "_llm"]).fill_null(False).mean()), 4) if both.height else None
        out[f"n_{f}"] = both.height
    out["agree_entry"] = round(float((j["entry"].struct.field("lo") == j["entry_llm"].struct.field("lo")).fill_null(False).mean()), 4)
    return out


def run(layout: Layout, *, llm_fixture: str | os.PathLike | None = None, ocr_fixture: str | os.PathLike | None = None, ingested_at: datetime | None = None) -> dict[str, Any]:
    ingested_at = ingested_at or now_utc()
    mv = pl.read_parquet(layout.message_version)
    dg = pl.read_parquet(layout.duplicate_group) if layout.duplicate_group.exists() else None
    client = RecordedClient.from_file(llm_fixture) if llm_fixture else None
    ocr = RecordedOcr.from_file(ocr_fixture) if ocr_fixture else None
    df, qrows, ledgers, summary = extract_frame(mv, dg, client=client, ocr=ocr, ingested_at=ingested_at)
    layout.ensure()
    old = pl.read_parquet(layout.extracted_event) if layout.extracted_event.exists() else None
    write_parquet_atomic(preserve_ingested_at(df, old, "extract_id"), layout.extracted_event)
    append_quarantine(layout.quarantine_path, qrows)
    batch_id = summary["batch_id"]
    lrows, maps = [], []
    for key in sorted(ledgers, key=lambda k: (k[0], k[1])):
        row, _ = loss_row_from_ledger(ledgers[key], batch_id=batch_id, cum_excluded_prev=cum_prev(layout, batch_id, (1, 2), ledgers[key].stratum), rule_version=RULE_VERSION, schema_hash_=SCHEMA_HASH, clocks=(df["event_time"].max() if df.height else None, df["available_at"].max() if df.height else None, ingested_at))
        lrows.append(row)
        maps += mapping_rows(ledgers[key], batch_id=batch_id, rule_version=RULE_VERSION, ingested_at=ingested_at)
    write_loss(layout.loss(batch_id), lrows, replace_layers={3})
    write_mapping(layout.mapping(batch_id, 3), maps)
    summary["paths"] = {"extracted_event": str(layout.extracted_event)}
    return summary


# ---------------------------------------------------------------- bench
def resolve_bench(path: str | os.PathLike) -> pathlib.Path:
    """bench 路径必须真实存在（目录含 dataset.json 或直接给 dataset.json）；不做静默回退（G0 要求显式报错）。"""
    p = pathlib.Path(path)
    if p.is_file():
        return p
    if (p / "dataset.json").is_file():
        return p / "dataset.json"
    raise FileNotFoundError(f"bench 不存在：{p}（期望目录含 dataset.json；仓库内真身为 eval/v3_trader_signal_bench/dataset.json）")


def _close(a: float | None, b: float | None, rel: float = 1e-6) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(Decimal(str(a)) - Decimal(str(b))) <= Decimal(str(rel)) * max(Decimal(1), abs(Decimal(str(b))))


def bench_action(res: ParseResult) -> str:
    if res.symbol_raw is None:
        return "skip"
    if res.kind == "entry_proposal" and res.side and Reason.INTENT_AMBIGUOUS not in res.reason_codes:
        if res.entry and res.entry["kind"] == "ladder" and len(res.entries) >= 1:
            return "open_batch"
        return "open"
    if res.kind == "stop_move" and res.stop is not None:
        return "set_sl"
    if res.kind == "reduce":
        return "partial"
    if res.kind == "close_claimed":
        return "close"
    return "skip"


def score_item(gold: dict[str, Any], res: ParseResult) -> dict[str, Any]:
    action = bench_action(res)
    accept = {gold["action"], *gold.get("also_accept_actions", [])}
    s: dict[str, Any] = {"pred_action": action, "action_ok": action in accept}
    if gold["action"] == "skip":
        s["strict_ok"] = s["action_ok"]
        return s
    sym_pred = (res.symbol_raw or "") + "USDT"
    s["symbol_ok"] = sym_pred == gold.get("symbol")
    s["side_ok"] = res.side == gold.get("side") if gold.get("side") else True
    et = gold.get("entry_type")
    if et == "zone":
        s["entry_ok"] = bool(res.entry) and res.entry["kind"] == "zone" and _close(res.entry["lo"], gold["price_min"]) and _close(res.entry["hi"], gold["price_max"])
    elif et == "limit" and gold.get("batch_entries"):
        s["entry_ok"] = bool(res.entries) and len(res.entries) == len(gold["batch_entries"]) and all(_close(a, b) for a, b in zip(res.entries, gold["batch_entries"]))
    elif et == "limit":
        s["entry_ok"] = bool(res.entry) and _close(res.entry["lo"], gold.get("price"))
    elif et == "market":
        s["entry_ok"] = res.entry is None or res.entry["kind"] == "market_ref"
    else:
        s["entry_ok"] = True
    if "sl" in gold or gold.get("sl_accept"):
        s["sl_ok"] = any(_close(res.stop, x) for x in gold.get("sl_accept", [gold.get("sl")]))
    else:
        s["sl_ok"] = res.stop is None
    if gold.get("tps"):
        g_tps = gold["tps"]
        p_tps = [t["level"] for t in res.tps]
        s["tps_ok"] = len(p_tps) >= 1 and all(any(_close(x, y) for y in g_tps) for x in p_tps) and all(any(_close(x, y) for x in p_tps) for y in g_tps)  # 预测⊆金标 且 金标⊆预测
    else:
        s["tps_ok"] = True
    s["strict_ok"] = all(v for k, v in s.items() if k.endswith("_ok"))
    return s


def run_bench(bench_path: str | os.PathLike, *, results_path: str | os.PathLike | None = None) -> dict[str, Any]:
    ds_path = resolve_bench(bench_path)
    ds = json.loads(ds_path.read_text(encoding="utf-8"))
    items = ds["items"]
    per: list[dict[str, Any]] = []
    for it in items:
        res = parse_message(it["text"])
        sc = score_item(it["gold"], res)
        per.append({"id": it["id"], "gold_action": it["gold"]["action"], "family": it["gold"].get("family"), "kind": res.kind, "symbol": res.symbol_raw, "side": res.side, **sc})
    actionable = [p for p in per if p["gold_action"] != "skip"]
    skips = [p for p in per if p["gold_action"] == "skip"]
    report: dict[str, Any] = {
        "bench": str(ds_path),
        "n_items": len(items),
        "n_actionable": len(actionable),
        "parser_version": PARSER_VERSION,
        "parser_recall": round(sum(p["action_ok"] for p in actionable) / max(1, len(actionable)), 4),
        "parser_strict_recall": round(sum(p["strict_ok"] for p in actionable) / max(1, len(actionable)), 4),
        "skip_recall": round(sum(p["action_ok"] for p in skips) / max(1, len(skips)), 4),  # 分母=真 skip（A02：这是召回不是精确率）
        "action_acc": round(sum(p["action_ok"] for p in per) / len(per), 4),
        "field_acc": {},
        "items": per,
    }
    for f in ("symbol_ok", "side_ok", "entry_ok", "sl_ok", "tps_ok"):
        vals = [p[f] for p in actionable if f in p]
        report["field_acc"][f] = round(sum(vals) / max(1, len(vals)), 4)
    # 字段级共错率：与已录制的 LLM 结果（results.json，真实模型的历史录制）对比，不发起任何调用
    rp = pathlib.Path(results_path) if results_path else ds_path.parent / "results.json"
    if rp.is_file():
        rs = json.loads(rp.read_text(encoding="utf-8"))
        gold_by = {it["id"]: it["gold"] for it in items}
        co: dict[str, Any] = {"source": str(rp), "configs": {}}
        for cfg, lst in rs.get("results", {}).items():
            by_id = {r["id"]: r for r in lst}
            both_wrong = {f: 0 for f in ("action", "symbol", "side", "entry", "sl")}
            n = {f: 0 for f in both_wrong}
            for p in per:
                r = by_id.get(p["id"])
                if not r or p["gold_action"] == "skip":
                    continue
                sc = r.get("score") or {}
                pairs = (("action", p["action_ok"], sc.get("action_ok")), ("symbol", p.get("symbol_ok"), sc.get("symbol_ok")),
                         ("side", p.get("side_ok"), sc.get("side_ok")), ("entry", p.get("entry_ok"), sc.get("entry_ok")), ("sl", p.get("sl_ok"), sc.get("sl_ok")))
                for f, a, b in pairs:
                    if a is None or b is None:
                        continue
                    n[f] += 1
                    if not a and not b:
                        both_wrong[f] += 1
            co["configs"][cfg] = {f: {"n": n[f], "both_wrong": both_wrong[f], "rate": round(both_wrong[f] / n[f], 4) if n[f] else None} for f in both_wrong}
        report["co_error_with_recorded_llm"] = co
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="层 3 抽取")
    ap.add_argument("--bench", help="bench 目录或 dataset.json")
    ap.add_argument("--report", help="bench 报告 JSON 输出路径")
    ap.add_argument("--results", help="bench 已录制模型结果（默认 dataset 同目录 results.json）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--out")
    g.add_argument("--lake-root")
    ap.add_argument("--llm-fixture", help="录制 LLM 夹具 json（不给则不产生 LLM 行）")
    ap.add_argument("--ocr-fixture", help="录制 OCR 夹具 json（不给则 OCR not_run）")
    a = ap.parse_args(argv)
    if a.bench:
        rep = run_bench(a.bench, results_path=a.results)
        if a.report:
            pathlib.Path(a.report).write_text(json.dumps(rep, ensure_ascii=False, indent=2))
        print(json.dumps({k: v for k, v in rep.items() if k != "items"}, ensure_ascii=False, indent=2))
        return 0
    layout = Layout.flat(a.out) if a.out else Layout.from_root(a.lake_root)
    print(json.dumps(run(layout, llm_fixture=a.llm_fixture, ocr_fixture=a.ocr_fixture), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
