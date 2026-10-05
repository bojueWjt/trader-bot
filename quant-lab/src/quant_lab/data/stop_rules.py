"""SKILL rule 9 stop derivation in silver (v8 F4) and relative-stop detection (F9).

Silver is the only place a stop is derived from wording. gold reads checks.stop_rule; live v4 trusts it.

The stop clause and price anchoring are copied from the live v3 profile
(market/live_profile.py:36-115: _ROLE/_ENTRY/_STOP/_TP, _sentences, _mentions, _GROUP, _NEAR_*,
_BREAK_BEFORE, _BREAK_AFTER, _BREAKOUT) so the data layer and live read the same clause. Runtime code
does not import across layers; tests/data/test_stop_rules.py and the G2 tests compare the two copies.

Fuzzy wording is tied to the stop it modifies: step 1 counts a fuzzy phrase that carries a number only when
that number is the stop (v3 scales or the inherited unit), and step 3 counts a fuzzy phrase only when stop
wording sits in its comma clause or the phrase lies in a v3 stop clause. A model stop equal to an entry that
derives nothing is cleared (stop None + nostop_hint), never kept at the entry.

derive_stop records, on the ParseResult:
  checks.stop_rule = {rule, base, base_source, widened, span, version}
      rule ∈ r9_fuzzy_break / plain_break / chart / close_from_clause
  checks.nostop_hint ∈ zero_distance_break / reference_level / ambiguous_break / break_wrong_side /
      close_like / close_like_kept_price
detect_relative records checks.stop_relative = {kind: points|pct, value, quote, span, ref} or
  checks.nostop_hint = relative_ambiguous; the price is resolved where the mark is known (validate).
"""
from __future__ import annotations

from decimal import Decimal
import math
import re

STOP_RULES_VERSION = "stop-rules-v2"  # v2: 「走势/走向/走弱…」 are not stop wording; plain breaks must be anchored
WIDEN = Decimal("0.003")

# ---------------------------------------------------------------- live v3 copy (live_profile.py:36-100)
_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])\d+(?:,\d{3})*(?:\.\d+)?(?![A-Za-z0-9_.])")
_ROLE = re.compile(r"入场|进场|开多|开空|做多|做空|多单|空单|止损|停损|失效|止盈|目标|(?<!\w)(?:entry|sl|stop|tp\d*|target\d*)(?!\w)", re.I)
_ENTRY = re.compile(r"入场|进场|开多|开空|做多|做空|多单|空单|(?<!\w)entry(?!\w)", re.I)
_STOP = re.compile(r"止损|停损|失效|(?<!\w)(?:sl|stop)(?!\w)", re.I)
_TP = re.compile(r"止盈|目标|(?<!\w)(?:tp|target)\d*(?!\w)", re.I)
_FUZZY = re.compile(r"附近|左右|大约|(?<![合预条签公解履邀])约(?![定束会翰]|\s*\d+(?:\.\d+)?\s*%)")
_BREAKOUT = re.compile(r"略破|小幅突破|稍微超过|小幅跌破|小幅涨破|稍微跌破|稍微涨破")
_NUM = r"\d+(?:,\d{3})*(?:\.\d+)?"
_UNIT = r"(?:万|千|[kKwW](?![A-Za-z]))"
_GROUP = re.compile(rf"(?<![A-Za-z0-9_.]){_NUM}\s*{_UNIT}?(?:\s*(?:-|–|—|~|～|到|至|、|，|和|及|/)\s*\$?\s*{_NUM}\s*{_UNIT}?)*(?![A-Za-z0-9_.%])")
_SCALES = (1, 100, 1000, 10000)
_CURRENCY = r"(?:美元|美金|USDT|USD|刀|块|点(?!位)|\$|U(?![A-Za-z]))"
_LEVEL_NOUN = r"(?:支撑|阻力|压力|区域|区间|位置|价位|点位|一线|关口|大关)"
_NEAR_AFTER = re.compile(rf"\s*{_CURRENCY}?\s*[)）]?\s*{_LEVEL_NOUN}?\s*(?:附近|左右|上下|一带)")
_NEAR_BEFORE = re.compile(r"(?:大约|大概|(?<![合预条签公解履邀])约)\s*(?:在|为|是)?\s*\$?\s*$")
_BREAK_BEFORE = re.compile(r"(?:略破|小幅突破|稍微超过|小幅跌破|小幅涨破|稍微跌破|稍微涨破)\s*\$?\s*$")
_BREAK_AFTER = re.compile(rf"\s*{_UNIT}?\s*{_CURRENCY}?\s*一点")


def _clauses(text: str) -> list[tuple[int, int, str]]:
    """live v3 _sentences with each clause's offset in text: (line_no, start, clause)."""
    clauses = []
    offset = 0
    for line_no, (body, line) in enumerate(zip(text.splitlines(), text.splitlines(True)), 1):
        position = 0
        for sentence in re.split(r"[;；。!?！？]", body):
            base = offset + position
            labels = list(_ROLE.finditer(sentence))
            start, role = 0, None
            if labels and labels[0].start() > 0:
                clauses.append((line_no, base, sentence[:labels[0].start()]))
                start = labels[0].start()
            for match in labels:
                if _ENTRY.fullmatch(match.group()):
                    current = "entry"
                elif _STOP.fullmatch(match.group()):
                    current = "stop"
                else:
                    current = "tp"
                if role is not None and (current != role or current == "tp"):
                    clauses.append((line_no, base + start, sentence[start:match.start()]))
                    start = match.start()
                role = current
            if sentence[start:].strip():
                clauses.append((line_no, base + start, sentence[start:]))
            position += len(sentence) + 1
        offset += len(line)
    return clauses


def _sentences(text: str) -> list[tuple[int, str]]:
    """Same clauses as live v3 _sentences."""
    return [(line, clause) for line, _, clause in _clauses(text)]


def _mentions(clause: str) -> list[tuple[set, bool, bool]]:
    out = []
    for m in _GROUP.finditer(clause):
        raw = [Decimal(x.replace(",", "")) for x in re.findall(_NUM, m.group())]
        before = clause[:m.start()]
        out.append(({v * k for v in raw for k in _SCALES},
                    bool(_NEAR_AFTER.match(clause, m.end()) or _NEAR_BEFORE.search(before)),
                    bool(_BREAK_BEFORE.search(before) or _BREAK_AFTER.match(clause, m.end()))))
    return out


def _role(clause: str) -> str | None:
    return "entry" if _ENTRY.search(clause) else "stop" if _STOP.search(clause) else "tp" if _TP.search(clause) else None


def _symbol_ok(line: str, symbol: str | None) -> bool:
    if not symbol:
        return True
    base = re.sub(r"(?:USDT|USDC|BUSD)$", "", symbol)
    tokens = set(re.findall(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]{1,14}(?![A-Za-z0-9])", line))
    tokens = {t for t in tokens if not re.fullmatch(r"(?:SL|TP|TARGET)\d*|ENTRY|STOP|LONG|SHORT|USDT|USDC", t)}
    return not tokens or tokens.issubset({symbol, base, base + "USDT"})


def _v3_clauses(text: str, symbol: str | None = None) -> list[dict]:
    lines = text.splitlines()
    out = []
    for line_no, start, clause in _clauses(text):
        if not _symbol_ok(lines[line_no - 1], symbol):
            continue
        numbers = {Decimal(m.group().replace(",", "")) * k for m in _NUMBER.finditer(clause) for k in _SCALES}
        out.append({"line": line_no, "start": start, "text": clause, "role": _role(clause), "numbers": numbers,
                    "mentions": _mentions(clause)})
    return out


def stop_clauses(text: str, symbol: str | None = None) -> list[dict]:
    """Stop-labelled clauses, same range as live v3: {line, start, text}."""
    return [{k: c[k] for k in ("line", "start", "text")} for c in _v3_clauses(text, symbol) if c["role"] == "stop"]


def breakout_on(price, text: str | None, role: str = "stop", symbol: str | None = None) -> bool:
    """live v3 wording_flags level(): breakout wording on this stop price (always False for other roles)."""
    if role != "stop" or price is None:
        return False
    prices = {Decimal(str(price))}
    clauses = _v3_clauses(text or "", symbol)
    labelled = [c for c in clauses if c["role"] == "stop" and prices <= c["numbers"]]
    if len(labelled) > 1:
        return False
    if labelled:
        c = labelled[0]
        return bool(_BREAKOUT.search(c["text"])) or any(prices & values and broke for values, _, broke in c["mentions"])
    pool = [m for c in clauses for m in c["mentions"]]
    hits = [[m for m in pool if p in m[0]] for p in prices]
    return bool(hits) and all(any(m[2] for m in found) for found in hits)


# ---------------------------------------------------------------- v8 rule 9 wording
NUM = r"\$?\s*\d+(?:,\d{3})*(?:\.\d+)?(?:\s*[万wW]\s*\d{1,4}(?:\s*千)?(?!\d)|\s*(?:万|[wWkK](?![A-Za-z])))?"
REFERENCE = r"前低|前高|新高|新低|针尖|颈线|趋势线|均线|上沿|下沿|平台|盘整"
REFERENCE_WORDS = re.compile(REFERENCE)
_LONG_BREAK = {"跌破", "跌穿", "破位", "破"}
_SHORT_BREAK = {"涨破", "突破", "升破", "超过", "破"}
# 略 inside a word (策略/战略/忽略/省略...) is not the adverb 'slightly'.
_SLIGHTLY = r"(?:小幅|(?<![策战忽省攻谋侵粗概简领约])略微?|稍微?|一点点?)"
FUZZY_BREAK = re.compile(
    rf"{_SLIGHTLY}\s*(?P<verb>跌破|跌穿|破位|涨破|突破|升破|超过|破)\s*(?:的)?\s*(?:{REFERENCE})?\s*(?P<n>{NUM})?"
    rf"|(?P<verb2>跌破|涨破|突破|破)\s*(?P<n2>{NUM})\s*(?:万|[wWkK])?\s*一点")
# 「走」 only as the verb 'leave': 走势/走强/走弱/走高/走低/走到/走出/走向/走完 are about the market, not a stop
# (same lookahead as STOP_ANCHOR). 「跌破68000走势就转弱」 states no stop.
_LEAVE = r"走(?![势强弱高低到出向完])"
PLAIN_BREAK_STOP = re.compile(
    rf"(?P<verb>跌破|跌穿|涨破|突破|升破|破)\s*(?:{REFERENCE})?\s*(?P<n>{NUM})?\s*(?:附近|左右|一带|上下|一线)?\s*(?:就|则|后)?\s*"
    rf"(?:小幅|略微?|稍微?)?\s*(?:止损|离场|走人|{_LEAVE}|认错|出局|小损)")
# Any break ... stop wording in one clause, used only to keep close/indicator conditions out of intraday stops.
LOOSE_BREAK_STOP = re.compile(r"(?:跌破|跌穿|涨破|突破|升破|破位|破)[^。；;！!？?\n]{0,16}?(?:止损|离场|走人|出局|认错)")
CLOSE_COND = re.compile(r"收盘|收线|收于|收在|收回|收不回|站不回|站稳|有效跌破|有效突破|有效站上|确认|\d+\s*根")
INDICATOR = re.compile(r"均线|EMA|MA\d+|布林|趋势线", re.I)
TIMEFRAME = re.compile(r"日线|周线|小时线|\d+\s*(?:分钟|小时|min|h|H|d|D)\s*(?:K|k)?线?")
_CLAUSE_SPLIT = re.compile(r"[，,。；;！!？?\n]")
# Step 3 counts a fuzzy break only when it is about a stop: stop wording in its comma clause, or inside a v3
# stop clause. 「回踩小幅跌破68000是上车机会」「小幅跌破68000再补一单」 are entries, not stops.
STOP_ANCHOR = re.compile(r"止损|停损|离场|走人|认错|出局|防守|小损|(?:就|则|后|即|直接)\s*走(?![势强弱高低到出向完])")
# Step 2 derives from a priced condition only when the clause says the level breaks (「止损看68000附近的反应」 does not).
BREAK_VERB = re.compile(r"跌破|跌穿|破位|涨破|突破|升破|超过|破")


def _comma_clause(text: str, start: int, end: int) -> tuple[int, int]:
    lo = max((m.end() for m in _CLAUSE_SPLIT.finditer(text, 0, start)), default=0)
    hi = next((m.start() for m in _CLAUSE_SPLIT.finditer(text, end)), len(text))
    return lo, hi


def _number(raw: str | None) -> Decimal | None:
    """Value of one NUM capture with the F5a tokenizer (X万Y, 万/k)."""
    if not raw:
        return None
    from . import cx_v2
    raw = raw.replace("$", "").strip()
    best = None
    for value, start, end, pct in cx_v2.tokens(raw):
        if not pct and (best is None or end - start > best[1] - best[0]):
            best = (start, end, value)
    return best[2] if best else None


def _inherit(n: Decimal, quote: str, action) -> Decimal:
    """A phrase number with this action's inherited unit (cx_v2.inherited_prices), else itself."""
    if not action:
        return n
    from . import cx_v2
    probe = dict(action, stop={"kind": "price", "price": {"value": str(n), "quote": quote.replace("$", "").strip()}, "condition": None})
    try:
        inherited, _ = cx_v2.inherited_prices(probe)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        inherited = {}
    return inherited.get("stop.price", n)


def _phrase_values(raw: str, action) -> set[Decimal]:
    """Every price one phrase number can stand for: v3 _SCALES and the action's inherited unit."""
    n = _number(raw)
    if n is None:
        return set()
    return {n * k for k in _SCALES} | {_inherit(n, raw, action)}


def _fuzzy_on_stop(clause: str, stop: Decimal, action) -> bool:
    """Fuzzy break wording in this stop clause that is about this stop: numberless, or its number is the stop.
    「止损68000，小幅跌破65000加仓」: the fuzzy wording belongs to 65000, not to the 68000 stop."""
    for m in FUZZY_BREAK.finditer(clause):
        raw = m["n"] or m["n2"]
        if raw is None or stop in _phrase_values(raw, action):
            return True
    return False


def _breakout_on_stop(stop: Decimal, text: str, symbol, cl, action) -> bool:
    """live v3 breakout_on, except that a labelled clause's breakout word carrying a different number is not
    about this stop (v3 reads 「止损68000，小幅跌破65000加仓」 as a fuzzy 68000; silver does not)."""
    if cl and _fuzzy_on_stop(cl["text"], stop, action):
        return True
    if not breakout_on(stop, text, symbol=symbol):
        return False
    labelled = [c for c in _v3_clauses(text, symbol) if c["role"] == "stop" and stop in c["numbers"]]
    if len(labelled) != 1:
        return True  # v3 found it in the mention pool, which is anchored to the stop's own number group
    c = labelled[0]
    if any(stop in values and broke for values, _, broke in c["mentions"]):
        return True
    return _fuzzy_on_stop(c["text"], stop, action)


# ---------------------------------------------------------------- action paragraph
_SYMBOL_TOKEN = re.compile(r"(?<![A-Za-z])[#$]?([A-Z]{2,10})(?![A-Za-z])")
_NOT_SYMBOLS = {"CMP", "DCA", "ENTRY", "STOP", "TARGET", "BTCD", "USDC", "BUSD", "OI", "KDJ", "MACD", "BOLL", "VWAP", "LONG", "SHORT",
                # analysis vocabulary, not coins: it must not cut a paragraph (「BTC 7万多 FOMC 前小幅跌破就止损」)
                "FOMC", "OB", "FVG", "IFVG", "BOS", "SMC", "ICT", "CHOCH", "MSS", "MSB", "BPR", "OTE", "POC", "VAH", "VPVR",
                "SR", "RR", "ATR", "SMA", "FIB", "BB", "HH", "HL", "LH", "LL", "EQH", "EQL", "PDH", "PDL", "PWH", "PWL",
                "DXY", "SPX", "NY", "US", "CN", "EU", "UK", "NO", "YES", "KOL", "CEO", "PPI"}


def symbol_mentions(text: str) -> list[tuple[int, int, str]]:
    from .extract import SYMBOL_ALIASES, SYMBOL_STOP, canonical_symbol
    out = []
    for m in _SYMBOL_TOKEN.finditer(text):
        token = m.group(1)
        if token in SYMBOL_STOP or token in _NOT_SYMBOLS:
            continue
        out.append((m.start(), m.end(), canonical_symbol(token)))
    for alias, code in SYMBOL_ALIASES.items():
        if len(alias) < 2 or alias.isascii():
            continue
        for m in re.finditer(re.escape(alias), text):
            out.append((m.start(), m.end(), code))
    return sorted(out)


def _spans(action) -> list[tuple[int, int]]:
    return [(int(s["start"]), int(s["end"])) for s in (action.get("spans") or []) if s.get("source", "text") == "text"]


def segment(text: str, action: dict, siblings=()) -> tuple[int, int] | None:
    """This action's paragraph: the lines its spans cover plus at most two following lines, cut at another
    symbol or another action's anchor. Without spans the paragraph starts at the action's own symbol."""
    from .extract import canonical_symbol
    own = canonical_symbol(action.get("symbol_raw"))
    mentions = symbol_mentions(text)
    spans = _spans(action)
    other_spans = sorted(s for sib in siblings for s in _spans(sib) if s not in spans)
    if spans:
        lo, hi = min(s for s, _ in spans), max(e for _, e in spans)
    else:
        mine = [m for m in mentions if m[2] == own]
        if mine:
            lo = hi = mine[0][0]
        elif not any(m[2] != own for m in mentions) and not other_spans:
            return 0, len(text)
        else:
            return None
    starts = [0] + [m.end() for m in re.finditer(r"\n", text)]
    ends = [m.start() for m in re.finditer(r"\n", text)] + [len(text)]
    first = max(i for i, s in enumerate(starts) if s <= lo)
    last = max(i for i, s in enumerate(starts) if s <= max(lo, hi - 1))
    a, b = starts[first], ends[min(last + 2, len(ends) - 1)]
    for start, end, code in mentions:
        if code != own:
            if start >= hi:
                b = min(b, start)
            elif end <= lo:
                a = max(a, end)
    for start, end in other_spans:
        if start >= hi:
            cut = max((m.end() for m in _CLAUSE_SPLIT.finditer(text, hi, start)), default=start)
            b = min(b, cut)
        elif end <= lo:
            a = max(a, end)
    mine = [s for s, e, code in mentions if code == own and a <= s <= lo]
    if mine:
        a = max(mine)
    return a, max(a, b)


# ---------------------------------------------------------------- derivation
def _entry_values(result) -> list[Decimal]:
    values = [Decimal(str(v)) for v in result.entries or []]
    if not values and result.entry:
        values = [Decimal(str(result.entry["lo"])), Decimal(str(result.entry["hi"]))]
    return values


def _widen(base: Decimal, long: bool) -> Decimal:
    return base * (Decimal(1) - WIDEN if long else Decimal(1) + WIDEN)


def _correct_side(stop: Decimal, entries: list[Decimal], long: bool) -> bool:
    return all(stop < e for e in entries) if long else all(stop > e for e in entries)


def _price_clause(text: str, price: Decimal, symbol: str | None) -> dict | None:
    """The clause that states this stop price, v3 range. A label after the number (「跌破145就止损」)
    joins the clause holding the number with the stop clause that follows on the same line."""
    from . import cx_v2
    clauses = _v3_clauses(text, symbol)
    symbols = cx_v2.canonical_symbols([symbol])
    for c in clauses:
        c["numbers"] |= {v for v, _, _, pct in cx_v2.tokens(c["text"], symbols) if not pct}
    labelled = [c for c in clauses if c["role"] == "stop" and price in c["numbers"]]
    if len(labelled) == 1:
        c = labelled[0]
        return {"start": c["start"], "text": c["text"]}
    if labelled:
        return None
    found = [i for i, c in enumerate(clauses) if price in c["numbers"]]
    if len(found) != 1:
        return None
    i = found[0]
    c = dict(clauses[i])
    following = clauses[i + 1] if i + 1 < len(clauses) else None
    if following and following["role"] == "stop" and following["line"] == c["line"] and following["start"] == c["start"] + len(c["text"]):
        c["text"] += following["text"]
    return {"start": c["start"], "text": c["text"]}


def _record(result, rule, base, base_source, widened, span, **extra):
    record = {"rule": rule, "base": None if base is None else str(base), "base_source": base_source,
              "widened": str(WIDEN) if widened else "0", "span": list(span) if span else None, "version": STOP_RULES_VERSION}
    record.update(extra)
    result.checks["stop_rule"] = record


def _hint(result, hint):
    result.checks["nostop_hint"] = hint


def _is_open(result) -> bool:
    return result.kind == "entry_proposal" and result.checks.get("schema_version") == 2 and result.checks.get("op") == "open"


def derive_stop(result, text: str, siblings=()) -> None:
    """F4 for one v2 open (including a promoted one). Mutates result.stop and result.checks."""
    if not _is_open(result) or result.side not in ("long", "short"):
        return
    action = result.checks.get("action_original") or result.checks.get("action") or {}
    long = result.side == "long"
    entries = _entry_values(result)
    sibling_actions = [s.checks.get("action") or {} for s in siblings if s is not result and s.checks.get("schema_version") == 2]
    symbol = result.symbol_raw
    zero_distance = False
    if result.stop is not None:
        stop = Decimal(str(result.stop))
        if "stop" in (result.checks.get("chart_fill") or {}).get("fields", []):
            _record(result, "chart", stop, "chart", False, None)
            return
        zero_distance = stop in entries
        if not zero_distance:
            cl = _price_clause(text, stop, symbol)
            span = (cl["start"], cl["start"] + len(cl["text"])) if cl else None
            if cl and CLOSE_COND.search(cl["text"]):
                from .close_stop import parse_close_stop
                close = parse_close_stop(cl["text"], stop)
                if close is not None:
                    _record(result, "close_from_clause", stop, "quoted_number", False, span, timeframe=close["timeframe"])
                else:
                    _hint(result, "close_like_kept_price")
                return
            if _breakout_on_stop(stop, text, symbol, cl, action):
                result.stop = _widen(stop, long)
                _record(result, "r9_fuzzy_break", stop, "quoted_number", True, span)
                return
            if cl and PLAIN_BREAK_STOP.search(cl["text"]):
                _record(result, "plain_break", stop, "quoted_number", False, span)
            return
    source_stop = action.get("stop") or {}
    if source_stop.get("kind") == "condition" and result.stop is None:
        condition = source_stop.get("condition") or ""
        where = text.find(condition) if condition else -1
        clause = condition
        span = None
        if where >= 0:
            lo, hi = _comma_clause(text, where, where + len(condition))
            clause, span = text[lo:hi] + " " + condition, (where, where + len(condition))
        if CLOSE_COND.search(clause) or INDICATOR.search(clause) or TIMEFRAME.search(clause):
            return
        atom = source_stop.get("price")
        if atom is not None:
            if not BREAK_VERB.search(clause):
                return  # a priced condition with no break verb keeps the existing condition mapping (v7)
            factor = {u["field"]: Decimal(u["factor"]) for u in result.checks.get("unit_inherited", [])}
            base = Decimal(atom["value"]) * factor.get("stop.price", 1)
            fuzzy = bool(FUZZY_BREAK.search(condition) or _BREAKOUT.search(condition))
            stop = _widen(base, long) if fuzzy else base
            if entries and not _correct_side(stop, entries, long):
                _hint(result, "break_wrong_side")
                return
            result.stop = stop
            _record(result, "r9_fuzzy_break" if fuzzy else "plain_break", base, "quoted_number", fuzzy, span)
            return
    _derive_from_paragraph(result, text, action, sibling_actions, entries, long)
    if zero_distance and "stop_rule" not in result.checks:
        # The model's stop equals an entry and the wording derived nothing: that is no stop, not a stop at the
        # entry, so gold can send the row down the F1 no-stop path instead of a direction-check rejection.
        result.stop = None
        result.checks.setdefault("nostop_hint", "zero_distance_break")


def _derive_from_paragraph(result, text, action, siblings, entries, long):
    bounds = segment(text, dict(action, symbol_raw=result.symbol_raw), siblings)
    if bounds is None:
        return
    lo, hi = bounds
    seg = text[lo:hi]

    def condition_like(start, end):
        a, b = _comma_clause(text, lo + start, lo + end)
        clause = text[a:b]
        return CLOSE_COND.search(clause) or INDICATOR.search(clause) or TIMEFRAME.search(clause)

    for m in LOOSE_BREAK_STOP.finditer(seg):
        if condition_like(m.start(), m.end()):
            _hint(result, "close_like")
            return
    stop_spans = [(c["start"], c["start"] + len(c["text"])) for c in stop_clauses(text)]

    def anchored(start, end):
        a, b = _comma_clause(text, lo + start, lo + end)
        if STOP_ANCHOR.search(text[max(a, lo):min(b, hi)]):
            return True
        return any(s <= lo + start and lo + end <= e for s, e in stop_spans)

    hits = []
    for m in FUZZY_BREAK.finditer(seg):
        if not anchored(m.start(), m.end()):
            continue
        verb = m["verb"] or m["verb2"]
        raw = m["n"] or m["n2"]
        hits.append(dict(start=m.start(), end=m.end(), verb=verb, raw=raw, fuzzy=True,
                         n_span=(m.start("n"), m.end("n")) if m["n"] else (m.start("n2"), m.end("n2")) if m["n2"] else None))
    for m in PLAIN_BREAK_STOP.finditer(seg):
        if not anchored(m.start(), m.end()):
            continue
        hits.append(dict(start=m.start(), end=m.end(), verb=m["verb"], raw=m["n"], fuzzy=False,
                         n_span=(m.start("n"), m.end("n")) if m["n"] else None))
    if not hits:
        return
    # Overlapping phrases are one statement: 「小幅跌破就止损」 is both fuzzy and plain; the fuzzy reading wins.
    hits.sort(key=lambda h: (h["start"], -h["end"]))
    merged = []
    for h in hits:
        if merged and h["start"] < merged[-1]["end"]:
            last = merged[-1]
            last["fuzzy"] = last["fuzzy"] or h["fuzzy"]
            last["end"] = max(last["end"], h["end"])
            if last["raw"] is None and h["raw"] is not None:
                last["raw"], last["n_span"], last["verb"] = h["raw"], h["n_span"], h["verb"]
            continue
        merged.append(dict(h))
    for h in merged:
        if condition_like(h["start"], h["end"]):
            _hint(result, "close_like")
            return
    readings = []
    for h in merged:
        n = _number(h["raw"])
        if n is not None:
            readings.append((h, n, "phrase_number"))
            continue
        a, b = _comma_clause(text, lo + h["start"], lo + h["end"])
        if REFERENCE_WORDS.search(text[a:b]):
            readings.append((h, None, "reference_level"))
        else:
            readings.append((h, None, "entry"))
    bases = {(n, source) for _, n, source in readings}
    if len(bases) > 1:
        _hint(result, "ambiguous_break")
        return
    h, n, source = readings[0]
    fuzzy = any(r[0]["fuzzy"] for r in readings)
    verb = h["verb"]
    if verb not in (_LONG_BREAK if long else _SHORT_BREAK):
        _hint(result, "break_wrong_side")
        return
    if source == "reference_level":
        _hint(result, "reference_level")
        return
    span = (lo + h["start"], lo + h["end"])
    if source == "phrase_number":
        base = _inherit(n, seg[h["n_span"][0]:h["n_span"][1]], action) if h["n_span"] is not None else n
        base_source = "phrase_number"
    else:
        if not fuzzy:
            _hint(result, "zero_distance_break")
            return
        if entries:
            base = min(entries) if long else max(entries)
            base_source = "entry_low" if long else "entry_high"
        elif result.entry_mode == "market_ref":
            result.stop = None
            _record(result, "r9_fuzzy_break", None, "mark_at_t_a", True, span)
            return
        else:
            return
    stop = _widen(base, long) if fuzzy else base
    if entries and not _correct_side(stop, entries, long):
        _hint(result, "break_wrong_side")
        return
    result.stop = stop
    _record(result, "r9_fuzzy_break" if fuzzy else "plain_break", base, base_source, fuzzy, span)


def derive_stops(results, text: str) -> None:
    """Run derive_stop then detect_relative on every v2 open of one message."""
    for result in results:
        derive_stop(result, text, results)
        if result.stop is None and "stop_rule" not in result.checks and "nostop_hint" not in result.checks:
            detect_relative(result, text, results)


# ---------------------------------------------------------------- F9 relative stops
# 「带500u」 is usually a margin size, so 带 counts only with a points unit; 防守/止损/SL also take a currency unit.
RELATIVE_POINTS = re.compile(r"(?:防守|止损|SL)\s*(?P<v>\d+(?:\.\d+)?)\s*(?:个?点|点位|刀|u|U|美金)"
                             r"|带\s*(?P<vb>\d+(?:\.\d+)?)\s*(?:个?点|点位)", re.I)
RELATIVE_MIN, RELATIVE_MAX = Decimal("0.002"), Decimal("0.15")
RELATIVE_PCT = re.compile(r"(?:止损|SL|stop|最大损失)\D{0,6}?(?P<v>\d+(?:\.\d+)?)\s*%?(?:\s*(?:至|到|-|~|～)\s*(?P<v2>\d+(?:\.\d+)?))?\s*%", re.I)
LEVERAGE = re.compile(r"\d+\s*(?:倍|x|X)|杠杆")
_RELATIVE_SPLIT = re.compile(r"[。；;，,\n]")


def detect_relative(result, text: str, siblings=()) -> None:
    """F9 detection only: write checks.stop_relative, or relative_ambiguous; never a price."""
    if not _is_open(result) or result.stop is not None or result.side not in ("long", "short"):
        return
    action = result.checks.get("action") or {}
    sibling_actions = [s.checks.get("action") or {} for s in siblings if s is not result and s.checks.get("schema_version") == 2]
    bounds = segment(text, dict(action, symbol_raw=result.symbol_raw), sibling_actions)
    if bounds is None:
        return
    lo, hi = bounds
    found = []
    for kind, pattern in (("points", RELATIVE_POINTS), ("pct", RELATIVE_PCT)):
        for m in pattern.finditer(text, lo, hi):
            groups = m.groupdict()
            value = Decimal(groups["v"] if groups["v"] is not None else groups["vb"])
            if kind == "pct" and m["v2"]:
                value = max(value, Decimal(m["v2"]))
            a = max((x.end() for x in _RELATIVE_SPLIT.finditer(text, 0, m.start())), default=0)
            b = next((x.start() for x in _RELATIVE_SPLIT.finditer(text, m.end())), len(text))
            found.append(dict(kind=kind, value=value, quote=text[m.start():m.end()], span=[m.start(), m.end()],
                              leveraged=bool(LEVERAGE.search(text[a:b]))))
    if not found:
        return
    plain = [f for f in found if not f["leveraged"]]
    if len(plain) != 1:
        _hint(result, "relative_ambiguous")
        return
    found = plain[0]
    entries = _entry_values(result)
    ref = "entry_ref" if entries else "mark"
    if found["kind"] == "pct":
        distance = found["value"] / 100
    elif entries:
        distance = found["value"] / entries[0]
    else:
        distance = None  # points against the mark: resolve_relative applies the same gate once the mark is known
    if distance is not None and not (RELATIVE_MIN <= distance <= RELATIVE_MAX):
        _hint(result, "relative_ambiguous")
        return
    result.checks["stop_relative"] = {"kind": found["kind"], "value": str(found["value"]), "quote": found["quote"],
                                      "span": found["span"], "ref": ref, "version": STOP_RULES_VERSION}


def resolve_relative(relative: dict, side: str, ref: Decimal) -> Decimal | None:
    """Price of a detected relative stop once its reference (entry or as-of mark) is known.

    None when the distance from ref is outside [0.2%, 15%] (or ref is not positive): the caller records
    relative_ambiguous. The gate lives here so a mark-referenced points value (「止损65000u」 read as 65000
    points) cannot be priced by a caller that skips it."""
    value, ref = Decimal(relative["value"]), Decimal(str(ref))
    if ref <= 0:
        return None
    long = side == "long"
    if relative["kind"] == "points":
        stop = ref - value if long else ref + value
    else:
        stop = ref * (Decimal(1) - value / 100) if long else ref * (Decimal(1) + value / 100)
    if not (RELATIVE_MIN <= abs(ref - stop) / ref <= RELATIVE_MAX):
        return None
    return stop
