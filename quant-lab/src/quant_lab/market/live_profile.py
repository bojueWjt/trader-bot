"""Pure trader-v3 execution-plan transform; never changes the source plan or parser.

Committed production references (paths relative to trader-bot):
  hermes-profile/skills/trading/v3-trader/SKILL.md:19,27,34
  services/control-plane/api/read_api.py:545-549,746-784,786-804
  packages/execution-domain/execution_domain/entry_batch.py:19-56

Entry fractions in kernel A are *quantity* fractions. A zone therefore needs
weights proportional to risk_share / stop_distance; explicit pairs need 1/price.
The common risk budget, lot rounding, wallet and fill rules remain kernel A's.

v4 (v8 F4): rows from a v8 graph carry a ``stop_rule`` key (value may be empty)
and the stop is read from the silver derivation instead of re-deriving it here:
r9_fuzzy_break was already widened 0.3% upstream, so live forces only the 0.1%;
close_from_clause gets no widening; every other rule widens 0.1% only when a
fuzzy word sits on ``stop_base`` in the stop message text (breakout wording off).
Rows without the key (v7 graphs) keep the v3 behaviour byte for byte. A plan
without a stop (v8 F1) skips the stop entirely; entries and targets are unchanged.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_DOWN
import re

from quant_lab.market.contract import ContractError, D, RATIO_QUANTUM

PROFILE_VERSION = "trader-v3-live-v4"   # v2: SL/TP 0.1% only with fuzzy wording; v3: wording attached to the price itself, no label needed;
                                        # v4: v8 rows read the silver stop_rule (r9 widened upstream, live adds only 0.1%)
SOURCES = [
    "hermes-profile/skills/trading/v3-trader/SKILL.md:19",
    "hermes-profile/skills/trading/v3-trader/SKILL.md:27",
    "hermes-profile/skills/trading/v3-trader/SKILL.md:34",
    "services/control-plane/api/read_api.py:545-549",
    "services/control-plane/api/read_api.py:746-804",
    "packages/execution-domain/execution_domain/entry_batch.py:19-56",
]
ZONE_TRANCHES = (
    ("t1_near", Decimal("0"), Decimal("0.55")),
    ("t2_mid", Decimal("0.50"), Decimal("0.30")),
    ("t3_deep", Decimal("0.85"), Decimal("0.15")),
)
RULES = ("entry_concession", "stop_breakout", "stop_widening", "take_profit_concession",
         "zone_ladder", "equal_notional_pair", "tick_rounding")
_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])\d+(?:,\d{3})*(?:\.\d+)?(?![A-Za-z0-9_.])")
_ROLE = re.compile(r"入场|进场|开多|开空|做多|做空|多单|空单|止损|停损|失效|止盈|目标|(?<!\w)(?:entry|sl|stop|tp\d*|target\d*)(?!\w)", re.I)
_ENTRY = re.compile(r"入场|进场|开多|开空|做多|做空|多单|空单|(?<!\w)entry(?!\w)", re.I)
_STOP = re.compile(r"止损|停损|失效|(?<!\w)(?:sl|stop)(?!\w)", re.I)
_TP = re.compile(r"止盈|目标|(?<!\w)(?:tp|target)\d*(?!\w)", re.I)
# 让点只看措辞（用户约定）：入场、止损、止盈的点位带这些字才让 0.1%；精确点位原值执行。
_FUZZY = re.compile(r"附近|左右|大约|(?<![合预条签公解履邀])约(?![定束会翰]|\s*\d+(?:\.\d+)?\s*%)")
_BREAKOUT = re.compile(r"略破|小幅突破|稍微超过|小幅跌破|小幅涨破|稍微跌破|稍微涨破")   # 「一点」太常见，只认挂在止损价后面的
# 不靠标签的写法（「现价:58800附近」「等106660附近多」「约$114,150」「2810-2830附近」「875附近」=87500）：
# 模糊词直接挂在价格数字上。一组价格 = 一个或多个用区间/并列连接的数字（可带 $、千分位、万/k）；百分数不算价格。
_NUM = r"\d+(?:,\d{3})*(?:\.\d+)?"
_UNIT = r"(?:万|千|[kKwW](?![A-Za-z]))"
_GROUP = re.compile(rf"(?<![A-Za-z0-9_.]){_NUM}\s*{_UNIT}?(?:\s*(?:-|–|—|~|～|到|至|、|，|和|及|/)\s*\$?\s*{_NUM}\s*{_UNIT}?)*(?![A-Za-z0-9_.%])")
_SCALES = (1, 100, 1000, 10000)   # 写法简写：875→87500、123→123000、6.9万→69000
_CURRENCY = r"(?:美元|美金|USDT|USD|刀|块|点(?!位)|\$|U(?![A-Za-z]))"
_LEVEL_NOUN = r"(?:支撑|阻力|压力|区域|区间|位置|价位|点位|一线|关口|大关)"
_NEAR_AFTER = re.compile(rf"\s*{_CURRENCY}?\s*[)）]?\s*{_LEVEL_NOUN}?\s*(?:附近|左右|上下|一带)")
_NEAR_BEFORE = re.compile(r"(?:大约|大概|(?<![合预条签公解履邀])约)\s*(?:在|为|是)?\s*\$?\s*$")
_BREAK_BEFORE = re.compile(r"(?:略破|小幅突破|稍微超过|小幅跌破|小幅涨破|稍微跌破|稍微涨破)\s*\$?\s*$")
_BREAK_AFTER = re.compile(rf"\s*{_UNIT}?\s*{_CURRENCY}?\s*一点")


def _sentences(text: str) -> list[tuple[int, str]]:
    """Locate clauses within physical lines so an SL/TP qualifier cannot leak to entry.

    Adjacent entry labels belong to one opening clause. Separate entry clauses
    with the same prices are ambiguous and deliberately do not get a concession.
    Every target label starts its own clause ("TP1 130 TP2 140附近").
    No original text is returned in the audit.
    """
    clauses = []
    for line_no, line in enumerate(text.splitlines(), 1):
        for sentence in re.split(r"[;；。!?！？]", line):
            labels = list(_ROLE.finditer(sentence))
            start, role = 0, None
            if labels and labels[0].start() > 0:
                # Text before the first label is its own clause: 「52340附近多（止损51200）」 must not make the stop fuzzy.
                clauses.append((line_no, sentence[:labels[0].start()]))
                start = labels[0].start()
            for match in labels:
                if _ENTRY.fullmatch(match.group()):
                    current = "entry"
                elif _STOP.fullmatch(match.group()):
                    current = "stop"
                else:
                    current = "tp"
                if role is not None and (current != role or current == "tp"):
                    clauses.append((line_no, sentence[start:match.start()]))
                    start = match.start()
                role = current
            if sentence[start:].strip():
                clauses.append((line_no, sentence[start:]))
    return clauses


def _mentions(clause: str) -> list[tuple[set, bool, bool]]:
    """Price groups in a clause: candidate values and whether approx / breakout wording sits on the number."""
    out = []
    for m in _GROUP.finditer(clause):
        raw = [D(x.replace(",", "")) for x in re.findall(_NUM, m.group())]
        before = clause[:m.start()]
        out.append(({v * k for v in raw for k in _SCALES},
                     bool(_NEAR_AFTER.match(clause, m.end()) or _NEAR_BEFORE.search(before)),
                     bool(_BREAK_BEFORE.search(before) or _BREAK_AFTER.match(clause, m.end()))))
    return out


def wording_flags(plan: dict, text: str | None, *, breakouts: bool = True) -> dict:
    """Price-anchored, line-based matching; missing/ambiguous evidence stays exact.

    A labelled clause (入场/止损/止盈…) holding the level decides it; two such
    clauses are ambiguous and stay exact. Without a labelled clause, the fuzzy
    word must sit on the price itself somewhere in the message (「现价:58800附近」).
    Lines naming another symbol, and openings on the other side, never count.
    """
    text = text or ""
    lines = text.splitlines()
    symbol = plan["instrument_id"].split("-", 1)[0]
    base_symbol = re.sub(r"(?:USDT|USDC|BUSD)$", "", symbol)
    long = plan["side"] == "long"
    opposite = r"开空|做空|空单|\bshort\b" if long else r"开多|做多|多单|\blong\b"
    same = r"开多|做多|多单|\blong\b" if long else r"开空|做空|空单|\bshort\b"

    def symbol_ok(line):
        # Labels are not symbols; an unrelated ETH opening at BTC's price is no evidence.
        tokens = set(re.findall(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]{1,14}(?![A-Za-z0-9])", line))
        tokens = {t for t in tokens if not re.fullmatch(r"(?:SL|TP|TARGET)\d*|ENTRY|STOP|LONG|SHORT|USDT|USDC", t)}
        return not tokens or tokens.issubset({symbol, base_symbol})

    def side_ok(line):
        # Label-free lines say the side with a bare 多/空 (「附近多」「附近空」); the opposite one disqualifies the line.
        against = re.search(opposite, line, re.I) or ("空" if long else "多") in line
        along = re.search(same, line, re.I) or ("多" if long else "空") in line
        return not (against and not along)

    clauses = []
    for line_no, clause in _sentences(text):
        if not symbol_ok(lines[line_no - 1]):
            continue
        role = "entry" if _ENTRY.search(clause) else "stop" if _STOP.search(clause) else "tp" if _TP.search(clause) else None
        numbers = {D(m.group().replace(",", "")) * k for m in _NUMBER.finditer(clause) for k in _SCALES}
        clauses.append({"line": line_no, "text": clause, "role": role, "numbers": numbers, "mentions": _mentions(clause),
                        "side_ok": not re.search(opposite, clause, re.I), "line_side_ok": side_ok(lines[line_no - 1])})

    def level(prices: set, role: str) -> tuple[bool, bool, int | None]:
        """(fuzzy, breakout, line) for one level written as these prices."""
        labelled = [c for c in clauses if c["role"] == role and prices <= c["numbers"] and (role != "entry" or c["side_ok"])]
        if len(labelled) > 1:
            return False, False, None
        if labelled:
            c = labelled[0]
            # Only wording on this number counts: a run-on clause may carry another price's 附近,
            # and one target clause may list several levels.
            fuzzy = any(prices & values and near for values, near, _ in c["mentions"])
            breakout = role == "stop" and breakouts and (bool(_BREAKOUT.search(c["text"]))
                                           or any(prices & values and broke for values, _, broke in c["mentions"]))
            return fuzzy or breakout, breakout, c["line"] if fuzzy or breakout else None
        pool = [(c["line"], m) for c in clauses if role != "entry" or c["line_side_ok"] for m in c["mentions"]]
        hits = [[(line, m) for line, m in pool if price in m[0]] for price in prices]
        fuzzy = bool(hits) and all(any(m[1] for _, m in found) for found in hits)
        breakout = role == "stop" and breakouts and bool(hits) and all(any(m[2] for _, m in found) for found in hits)
        line = next((line for found in hits for line, m in found if m[1] or m[2]), None)
        return fuzzy or breakout, breakout, line if fuzzy or breakout else None

    legs = [{D(e["price_lo"]), D(e["price_hi"] if e.get("price_hi") is not None else e["price_lo"])} for e in plan["entries"]]
    together = [c for c in clauses if c["role"] == "entry" and set().union(*legs) <= c["numbers"] and c["side_ok"]]
    entry_line = None
    if len(together) == 1:
        # One opening clause holds every leg (「入场100和110附近」「入场：6.85-6.9附近」): its wording applies to all.
        fuzzy, _, entry_line = level(set().union(*legs), "entry")
        entry_legs = [fuzzy] * len(legs)
    elif together:
        entry_legs = [False] * len(legs)
    else:
        entry_legs = []
        for prices in legs:
            fuzzy, _, line = level(prices, "entry")
            entry_legs.append(fuzzy)
            entry_line = entry_line or line
    stop_fuzzy, stop_breakout, stop_line = (level({D(plan["stop"]["price"])}, "stop") if plan.get("stop") is not None
                                            else (False, False, None))
    return {
        "entry_fuzzy": any(entry_legs),
        "entry_legs_fuzzy": entry_legs,
        "stop_fuzzy": stop_fuzzy,
        "stop_breakout": stop_breakout,
        "tp_fuzzy": [level({D(tp["level"])}, "tp")[0] for tp in plan.get("tps", [])],
        "entry_line": entry_line if any(entry_legs) else (together[0]["line"] if len(together) == 1 else None),
        "stop_line": stop_line,
    }


def _widen_stop(stop: Decimal, long: bool) -> Decimal:
    """The single 0.1% stop widening (v3 fuzzy stops and every v4 widening go through here)."""
    stop *= Decimal("0.999") if long else Decimal("1.001")
    return stop


#: v8 stop rules (silver checks.stop_rule.rule) that live v4 handles specially; all others are wording-only.
STOP_RULE_R9 = "r9_fuzzy_break"
STOP_RULE_CLOSE = "close_from_clause"


def v4_stop_widening(plan: dict, stop_meta: dict, text: str | None) -> tuple[bool, str]:
    """(widen 0.1%?, why) for a v8 row. Never adds the 0.3% breakout: silver already did it for r9."""
    rule = stop_meta.get("stop_rule") or None
    if rule == STOP_RULE_R9:
        return True, "r9_forced"
    if rule == STOP_RULE_CLOSE:
        return False, "close_trigger"
    base = stop_meta.get("stop_base")
    base = D(plan["stop"]["price"]) if base in (None, "") else D(base)
    probe = dict(plan, stop={"price": base})
    near = wording_flags(probe, text, breakouts=False)["stop_fuzzy"]
    return near, "near_wording" if near else "exact"


def round_price(price: Decimal, tick: Decimal, *, upward: bool) -> Decimal:
    if not tick.is_finite() or tick <= 0:
        raise ContractError("live profile requires a finite positive exchange tick")
    rounding = ROUND_CEILING if upward else ROUND_FLOOR
    value = (price / tick).to_integral_value(rounding=rounding) * tick
    if not value.is_finite() or value <= 0:
        raise ContractError("live profile price rounded to a nonpositive value")
    return value


def quantity_fractions(weights: list[Decimal]) -> list[Decimal]:
    total = sum(weights)
    if total <= 0 or any(w <= 0 for w in weights):
        raise ContractError("live profile requires positive allocation weights")
    parts = [(w / total).quantize(RATIO_QUANTUM, rounding=ROUND_DOWN) for w in weights]
    parts[-1] += Decimal(1) - sum(parts)
    if any(p <= 0 for p in parts):
        raise ContractError("live profile allocation rounded to zero")
    return parts


def apply_live_profile(plan: dict, text: str | None, tick_size: Decimal, *, enabled: bool = True,
                       stop_meta: dict | None = None, stop_text: str | None = None) -> tuple[dict, dict | None]:
    """Return a plan copy and a text-free audit. Disabled is an identity operation.

    Entry rounds up for long/down for short; SL and TP round down for long/up
    for short, as required by SKILL §9 (easier fill / later stop trigger).
    market_ref prices are sizing references: leave them alone, preserving market
    fills and the existing as-of/stale-quote handling in L0.
    stop_meta (v8 rows: {"stop_rule", "stop_base"}) selects the v4 stop rules; stop_text is the
    message holding the stop (falls back to the root text).
    """
    if not enabled:
        return plan, None
    tick = D(tick_size)
    flags = wording_flags(plan, text)
    long = plan["side"] == "long"
    out = deepcopy(plan)
    counts = dict.fromkeys(RULES, 0)

    def snapped(price, *, upward):
        value = round_price(D(price), tick, upward=upward)
        counts["tick_rounding"] += int(value != D(price))
        return value

    v4 = None
    if plan.get("stop") is not None:
        stop = D(plan["stop"]["price"])
        if stop_meta is None:
            if flags["stop_breakout"]:
                stop *= Decimal("0.997") if long else Decimal("1.003")
                counts["stop_breakout"] = 1
            if flags["stop_fuzzy"]:
                stop = _widen_stop(stop, long)
                counts["stop_widening"] = 1
        else:
            source = stop_text if stop_text is not None else text
            widen, why = v4_stop_widening(plan, stop_meta, source)
            if widen:
                stop = _widen_stop(stop, long)
                counts["stop_widening"] = 1
            v4 = {"stop_rule": stop_meta.get("stop_rule") or None, "decision": why,
                  "stop_text_resolved": stop_text is not None}
        out["stop"]["price"] = snapped(stop, upward=not long)
    for tp, fuzzy in zip(out.get("tps", []), flags["tp_fuzzy"]):
        price = D(tp["level"])
        if fuzzy:
            price = D(tp["level"]) * (Decimal("0.999") if long else Decimal("1.001"))
            counts["take_profit_concession"] += 1
        tp["level"] = snapped(price, upward=not long)

    entries, allocation = [], []
    for entry, fuzzy in zip(plan["entries"], flags["entry_legs_fuzzy"]):
        if entry["kind"] == "market_ref":
            entries.append(deepcopy(entry))
            continue
        # 每档入场看自己的措辞：「现价3680附近…补仓3740」只让第一档。
        factor = (Decimal("1.001") if long else Decimal("0.999")) if fuzzy else Decimal(1)
        lo, hi = D(entry["price_lo"]) * factor, D(entry["price_hi"]) * factor
        if entry["kind"] == "ladder":
            near, far = (hi, lo) if long else (lo, hi)
            for name, depth, risk_share in ZONE_TRANCHES:
                price = snapped(near + depth * (far - near), upward=long)
                entries.append(dict(entry, kind="limit", price_lo=price, price_hi=price))
                allocation.append({"tranche": name, "price": price, "risk_share": risk_share})
            counts["zone_ladder"] += 1
            counts["entry_concession"] += 3 if fuzzy else 0
        else:
            price = snapped(lo, upward=long)
            entries.append(dict(entry, price_lo=price, price_hi=price))
            counts["entry_concession"] += int(fuzzy)

    # One zone is the supported production shape; mixed zones need a separate
    # allocation contract and must not silently blend risk and quantity shares.
    if counts["zone_ladder"]:
        if len(plan["entries"]) != 1:
            raise ContractError("live profile supports one zone per opening")
        if out.get("stop") is None:
            raise ContractError("live profile zone allocation needs a stop (stopless zones are shaped to one limit first)")
        distances = [abs(a["price"] - out["stop"]["price"]) for a in allocation]
        if any(distance <= 0 for distance in distances):
            raise ContractError("live profile zone has zero stop distance after rounding")
        weights = [a["risk_share"] / distance for a, distance in zip(allocation, distances)]
        fractions = quantity_fractions(weights)
        for entry, fraction, record in zip(entries, fractions, allocation):
            entry["fraction"] = fraction
            record["quantity_fraction"] = fraction
    elif len(entries) == 2:
        fractions = quantity_fractions([Decimal(1) / D(e["price_lo"]) for e in entries])
        for entry, fraction in zip(entries, fractions):
            entry["fraction"] = fraction
        counts["equal_notional_pair"] = 1
    out["entries"] = entries
    audit = {"profile": PROFILE_VERSION, "status": "applied", "tick_size": tick,
             "wording": flags, "rule_counts": counts, "zone_allocation": allocation,
             "execution_plan": out}
    if v4 is not None:
        audit["stop_v4"] = v4
    return out, audit
