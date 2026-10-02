"""Pure trader-v3 execution-plan transform; never changes the source plan or parser.

Committed production references (paths relative to trader-bot):
  hermes-profile/skills/trading/v3-trader/SKILL.md:19,27,34
  services/control-plane/api/read_api.py:545-549,746-784,786-804
  packages/execution-domain/execution_domain/entry_batch.py:19-56

Entry fractions in kernel A are *quantity* fractions. A zone therefore needs
weights proportional to risk_share / stop_distance; explicit pairs need 1/price.
The common risk budget, lot rounding, wallet and fill rules remain kernel A's.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_DOWN
import re

from quant_lab.market.contract import ContractError, D, RATIO_QUANTUM

PROFILE_VERSION = "trader-v3-live-v2"   # v2: SL/TP 0.1% only with fuzzy wording, like entries
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
_FUZZY = re.compile(r"附近|左右|大约|(?<![合预条签公解履邀])约(?![定束会翰])")
_BREAKOUT = re.compile(r"略破|小幅突破|稍微超过|小幅跌破|小幅涨破|一点")


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


def wording_flags(plan: dict, text: str | None) -> dict:
    """Price-anchored, line-based matching; missing/ambiguous evidence stays exact.

    Only clauses with the plan's prices and an explicit role qualify. A bare
    number or prose elsewhere in the root message cannot enable fuzzy treatment.
    """
    clauses = _sentences(text or "")
    lines = (text or "").splitlines()
    symbol = plan["instrument_id"].split("-", 1)[0]
    base_symbol = re.sub(r"(?:USDT|USDC|BUSD)$", "", symbol)
    prices = {D(e[key]) for e in plan["entries"] for key in ("price_lo", "price_hi")}
    stop = D(plan["stop"]["price"])
    targets = [D(tp["level"]) for tp in plan.get("tps", [])]
    entry_candidates, stop_candidates = [], []
    target_candidates = [[] for _ in targets]
    for line_no, clause in clauses:
        # Explicit symbols on the same line must identify this opening. Labels
        # are not symbols; an unrelated ETH opening at BTC's price is no evidence.
        tokens = set(re.findall(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]{1,14}(?![A-Za-z0-9])", lines[line_no - 1]))
        tokens = {t for t in tokens if not re.fullmatch(r"(?:SL|TP|TARGET)\d*|ENTRY|STOP|LONG|SHORT|USDT|USDC", t)}
        if tokens and not tokens.issubset({symbol, base_symbol}):
            continue
        raw = {D(m.group().replace(",", "")) for m in _NUMBER.finditer(clause)}
        # Plans carry scaled prices ("6.14-6.19万" -> 61400/61900); every plan price must still match one written number.
        numbers = raw | {n * 1000 for n in raw} | {n * 10000 for n in raw}
        if _ENTRY.search(clause) and prices.issubset(numbers):
            opposite = r"开空|做空|空单|\bshort\b" if plan["side"] == "long" else r"开多|做多|多单|\blong\b"
            if not re.search(opposite, clause, re.I):
                entry_candidates.append((line_no, bool(_FUZZY.search(clause))))
        if _STOP.search(clause) and stop in numbers:
            stop_candidates.append((line_no, bool(_BREAKOUT.search(clause)), bool(_FUZZY.search(clause))))
        if _TP.search(clause) and not _ENTRY.search(clause) and not _STOP.search(clause):
            for found, level in zip(target_candidates, targets):
                if level in numbers:
                    found.append(bool(_FUZZY.search(clause)))
    entry = entry_candidates[0] if len(entry_candidates) == 1 else None
    sl = stop_candidates[0] if len(stop_candidates) == 1 else None
    return {
        "entry_fuzzy": entry is not None and entry[1],
        "stop_fuzzy": sl is not None and (sl[1] or sl[2]),
        "stop_breakout": sl is not None and sl[1],
        "tp_fuzzy": [len(found) == 1 and found[0] for found in target_candidates],
        "entry_line": entry[0] if entry is not None else None,
        "stop_line": sl[0] if sl is not None else None,
    }


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


def apply_live_profile(plan: dict, text: str | None, tick_size: Decimal, *, enabled: bool = True) -> tuple[dict, dict | None]:
    """Return a plan copy and a text-free audit. Disabled is an identity operation.

    Entry rounds up for long/down for short; SL and TP round down for long/up
    for short, as required by SKILL §9 (easier fill / later stop trigger).
    market_ref prices are sizing references: leave them alone, preserving market
    fills and the existing as-of/stale-quote handling in L0.
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

    stop = D(plan["stop"]["price"])
    if flags["stop_breakout"]:
        stop *= Decimal("0.997") if long else Decimal("1.003")
        counts["stop_breakout"] = 1
    if flags["stop_fuzzy"]:
        stop *= Decimal("0.999") if long else Decimal("1.001")
        counts["stop_widening"] = 1
    out["stop"]["price"] = snapped(stop, upward=not long)
    for tp, fuzzy in zip(out.get("tps", []), flags["tp_fuzzy"]):
        price = D(tp["level"])
        if fuzzy:
            price = D(tp["level"]) * (Decimal("0.999") if long else Decimal("1.001"))
            counts["take_profit_concession"] += 1
        tp["level"] = snapped(price, upward=not long)

    entries, allocation = [], []
    factor = Decimal(1)
    if flags["entry_fuzzy"]:
        factor = Decimal("1.001") if long else Decimal("0.999")
    for entry in plan["entries"]:
        if entry["kind"] == "market_ref":
            entries.append(deepcopy(entry))
            continue
        lo, hi = D(entry["price_lo"]) * factor, D(entry["price_hi"]) * factor
        if entry["kind"] == "ladder":
            near, far = (hi, lo) if long else (lo, hi)
            for name, depth, risk_share in ZONE_TRANCHES:
                price = snapped(near + depth * (far - near), upward=long)
                entries.append(dict(entry, kind="limit", price_lo=price, price_hi=price))
                allocation.append({"tranche": name, "price": price, "risk_share": risk_share})
            counts["zone_ladder"] += 1
            counts["entry_concession"] += 3 if flags["entry_fuzzy"] else 0
        else:
            price = snapped(lo, upward=long)
            entries.append(dict(entry, price_lo=price, price_hi=price))
            counts["entry_concession"] += int(flags["entry_fuzzy"])

    # One zone is the supported production shape; mixed zones need a separate
    # allocation contract and must not silently blend risk and quantity shares.
    if counts["zone_ladder"]:
        if len(plan["entries"]) != 1:
            raise ContractError("live profile supports one zone per opening")
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
    return out, audit
