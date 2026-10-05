"""v8 F2/F6：同一计划合并（D3），纯函数，不读写文件。

只合并同一计划的重发；可能是新计划的，一律交 G2 按「原单家族当时是否在场」判定（repost/amend）。
时钟（方案 §2）：Δ(C, T) = t_vis(C) − t_post(T)，t_post = 被比较那组最早成员的原帖可见时刻。

- 伴随帖（标题帖、图文对、中英双发；只补入场的补充）：0 < Δ ≤ 120 s 合并；
- 补止损（含解说帖 + 正式单，以及 linker 挂在根上的 stop_move/amend 补充）：Δ ≤ W_sup 合并（主口径 1800 s，-v8nw 为 0），
  超过只合成一条 move_stop，不回填；
- 不带参数的确认帖：回复/引用原单或 Δ ≤ 1800 s 永久 dup_of；其余以及 >1800 s 的 1% 内复述、完全相同的重发交 G2（repost）；
- 同一条消息的分支先判场所合并（same_message_venue），再判拆分（same_message_split），否则独立；
- 条件确认（C0）：不带参数的 C 回复条件帖 P（或不回复任何已知计划、同作者 Δ ≤ 1800 s），且 t_vis(C) − t_post(P) ≤ 86400 s
  → C 承接 P 的参数；
- 改单（amend）：C 回复限价/区间单 T、现价入场、无止损，且 C 的报价与当时 mark 都在 T 已经成交不了的一侧
  （多单高于 T 最高档、空单低于 T 最低档）→ 独立单，继承 T 的止损，合成 cancel_pending(T)；不要求价差 > 1%
  （方案测试 10：2270 挂单、现价 2285，差 0.66%，是改单）。
- 补充止损（F6）的 Δ 从该计划原帖（组内最早成员）算起，不从 linker 挂靠的那条复述算起；三道校验缺比较价时拒收（no_reference）。
- 冻结：一组一旦被 amend 或 repost 引用，它的字段与 t_dec 就定格。之后再到的补充止损（含 Δ ≤ W_sup 的）不再并进它，
  只合成一条 move_stop：组被改单引用时指向最近的那张改单（实际替换它的计划），否则指向该组保留单。否则原单的 t_dec
  会被推到改单/重发之后，G2 第二遍看不到原单当时在场，同一计划会执行两次，合成的撤单也会因早于原单 t_dec 被丢掉。

「可见截断」：目标组的所有成员都必须严格早于 C（Δ=0 时要求 sequence 能定先后），并且只用当时的字段。
stage 2 才使用分诊 relation（amends / reenters 覆盖确定性判定），并在保留单本口径不可执行时把指向它的
restatement / late_stop / repost / companion 翻成独立单。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

PLAN_MERGE_VERSION = "plan-merge-v2"  # v2: a group referenced by an amend/repost is frozen (stops after it become move_stop)
COMPANION_S = 120
DEFAULT_SUPPLEMENT_S = 1800
LINK_S = 1800
COMPATIBLE_LINK_S = 86400
SAME_REPOST_S = 60 * 86400
CONFIRM_S = 86400
REL_TOL = Decimal("0.01")
IDENT_TOL = Decimal("0.001")
SUPPLEMENT_LN = math.log(1.5)

KINDS = ("root", "companion", "restatement", "body_after_title", "image_text_pair", "commentary_formal", "supplement",
         "same_message_split", "same_message_venue", "conditional_confirmed", "repost", "amend", "reentry", "late_stop")
#: dup_of 的种类 → 原因码（companion 类 = 同一计划的伴随部分；restatement 类 = 复述或晚到止损）
COMPANION_KINDS = frozenset({"companion", "body_after_title", "image_text_pair", "commentary_formal", "supplement",
                             "same_message_split", "same_message_venue"})
RESTATEMENT_KINDS = frozenset({"restatement", "late_stop"})
#: stage 2：保留单不可执行时翻成独立单的种类
FLIP_KINDS = frozenset({"restatement", "late_stop", "repost", "companion"})

VENUE_WORDS = re.compile(r"现货|spot|币本位|1倍|一倍|低倍|长线|不设止损|不用止损|不会爆仓", re.I)
SPOT_WORDS = re.compile(r"现货|spot|只做现货|坚持现货|不做合约", re.I)
COIN_M_WORDS = re.compile(r"币本位")
REENTRY_WORDS = re.compile(r"重新(?:开|进|上车)|二次(?:进场|入场)|再次?(?:进场|入场|上车)|重开")
CARD_LABELS = re.compile(r"入场|止损|止盈|方向|Entry|SL|TP|目标", re.I)
PRICE_NUMBER = re.compile(r"\d")
CJK = re.compile(r"[一-鿿]")


def _ceil_s(seconds: float) -> int:
    """Reported whole seconds round up: a sub-second gap past a boundary is never shown inside it."""
    return math.ceil(seconds)


def whole_seconds(delta: timedelta) -> int:
    return _ceil_s(delta.total_seconds())


def venue_of(text: str) -> str:
    """Deterministic venue wording of one branch paragraph: spot / coin_m / unspecified."""
    if SPOT_WORDS.search(text or ""):
        return "spot"
    if COIN_M_WORDS.search(text or ""):
        return "coin_m"
    return "unspecified"


def is_card(text: str) -> bool:
    labels = {m.group().lower() for m in CARD_LABELS.finditer(text or "")}
    return len(labels) >= 2


@dataclass
class Item:
    """One entry branch (root), or one stop supplement linked to a root (supplement_of)."""
    pid: str
    svid: str
    channel: int
    message_id: int
    t_vis: datetime
    t_post: datetime | None = None
    author: str | None = None
    reply_to: int | None = None
    sequence: int | None = None
    inst: str | None = None
    side: str | None = None
    branch_index: int = 0
    legs: tuple[Decimal, ...] = ()
    market_ref: bool = False
    quote: Decimal | None = None  # a market_ref leg's quoted price (「现价 2285」); compared through the as-of mark
    mark: Decimal | None = None
    entry_kind: str | None = None
    stop: Decimal | None = None
    tps: tuple = ()
    text: str = ""
    segment: str = ""
    has_media: bool = False
    is_title: bool = False
    card: bool = False
    venue_words: bool = False
    reentry_words: bool = False
    supplement_of: str | None = None
    conditional: bool = False
    executable: bool = True
    relation: str | None = None
    relation_target: int | None = None

    def __post_init__(self):
        if self.t_post is None:
            self.t_post = self.t_vis

    @property
    def priced(self) -> bool:
        return bool(self.legs)

    @property
    def no_params(self) -> bool:
        return not self.legs and self.quote is None and self.stop is None

    @property
    def venue(self) -> str:
        return venue_of(self.segment or self.text)

    @property
    def lang(self) -> str:
        text = self.segment or self.text
        return "zh" if CJK.search(text or "") else "en"


@dataclass
class Group:
    gid: str
    channel: int
    inst: str | None
    side: str | None
    members: list[str]
    kept: str
    first_t: datetime
    first_seq: int | None
    entry_pid: str
    stop: Decimal | None
    stop_pid: str | None
    tps_pid: str | None
    providers: set[str]
    family: str
    last_t: datetime
    kind: str = "companion"


@dataclass
class Link:
    pid: str
    kind: str = "root"
    group: str | None = None
    kept: str | None = None
    dup_of: str | None = None
    repost_of: str | None = None
    amend_of: str | None = None
    reentry_of: str | None = None
    family: str | None = None
    target_message_id: int | None = None
    gap_s: int | None = None
    stop: Decimal | None = None
    stop_pid: str | None = None
    tps_pid: str | None = None
    providers: tuple[str, ...] = ()
    reentry_parent_stop: Decimal | None = None
    conditional_parent: str | None = None
    supplement_of: str | None = None
    supplement_rejected: str | None = None
    synthetic: list[dict[str, Any]] = field(default_factory=list)


def _rel(a: Decimal, b: Decimal) -> Decimal:
    return abs(a - b) / abs(b) if b else Decimal("Infinity")


def _cmp_legs(item: Item) -> list[Decimal]:
    """Priced legs; a market_ref-only item compares with its own as-of mark."""
    if item.legs:
        return list(item.legs)
    if item.market_ref and item.mark is not None:
        return [item.mark]
    return []


def _legs_match(c: list[Decimal], t: list[Decimal], tol: Decimal) -> bool:
    return all(any(_rel(x, y) <= tol for y in t) for x in c)


def compatible(c: Item, t: Item, *, t_stop: Decimal | None = None) -> bool:
    """Same channel/instrument/side; every priced leg of C within 1% of a leg of T; both stops within 1%."""
    if c.channel != t.channel or c.inst is None or c.inst != t.inst or c.side not in ("long", "short") or c.side != t.side:
        return False
    # A parameter-free post (「上车」) states no price: only instrument and side are compared. Otherwise an unpriced
    # side (market_ref without a mark) cannot conflict either.
    cl, tl = ([] if c.no_params else _cmp_legs(c)), _cmp_legs(t)
    if cl and tl and not _legs_match(cl, tl, REL_TOL):
        return False
    stop = t.stop if t_stop is None else t_stop
    if c.stop is not None and stop is not None and _rel(c.stop, stop) > REL_TOL:
        return False
    return True


def identical(c: Item, t: Item, *, t_stop: Decimal | None = None) -> bool:
    if c.channel != t.channel or c.inst is None or c.inst != t.inst or c.side != t.side:
        return False
    cl, tl = _cmp_legs(c), _cmp_legs(t)
    if not cl or not tl or not _legs_match(cl, tl, IDENT_TOL) or not _legs_match(tl, cl, IDENT_TOL):
        return False
    stop = t.stop if t_stop is None else t_stop
    if (c.stop is None) != (stop is None):
        return False
    return c.stop is None or _rel(c.stop, stop) <= IDENT_TOL


def venue_only_diff(c: Item, t: Item) -> bool:
    """Same message branches that differ only in venue wording (or not at all)."""
    return identical(c, t)


def _before(a: Item | Group, c: Item) -> bool:
    """a strictly visible before C: earlier t_vis, or equal t_vis with a known earlier sequence (linker.py:146)."""
    ta = a.last_t if isinstance(a, Group) else a.t_vis
    if ta < c.t_vis:
        return True
    if ta == c.t_vis:
        sa = None
        if isinstance(a, Group):
            sa = a.first_seq if len(a.members) == 1 else None
        else:
            sa = a.sequence
        return sa is not None and c.sequence is not None and sa < c.sequence
    return False


def _gap(c: Item, g: Group) -> float:
    return (c.t_vis - g.first_t).total_seconds()


def correct_side(stop: Decimal, legs: list[Decimal], side: str) -> bool:
    return all(stop < x for x in legs) if side == "long" else all(stop > x for x in legs)


def supplement_check(c: Item, root: Item, *, scale_conflict: bool = False) -> str | None:
    """F6 three gates for a stop supplement; returns the rejection reason or None."""
    if c.stop is None:
        return "no_stop"
    if scale_conflict:
        return "scale_not_unique"
    if c.inst is None:
        return "symbol_unknown"  # F11-D4 found no unique coin: the price has no checked unit
    if c.inst != root.inst:
        return "other_symbol"
    ref = _cmp_legs(root)
    if root.side not in ("long", "short"):
        return "side_unknown"
    if not ref:
        # A market_ref root without an as-of mark has no comparison price: neither the side nor the ln1.5 gate can be
        # checked, and an unchecked gate is not a passed one (uncomparable is not equal).
        return "no_reference"
    if not correct_side(c.stop, ref, root.side):
        return "wrong_side"
    mid = sum(ref) / len(ref)
    if c.stop <= 0 or mid <= 0 or abs(math.log(float(c.stop / mid))) >= SUPPLEMENT_LN:
        return "too_far"
    return None


def _past_the_limit(c: Item, t: Item) -> bool:
    """A market entry replied to a pending limit/zone plan T amends T when C's price is on the side T can no longer fill:
    a long above T's highest leg, a short below T's lowest. Both the quoted price and the as-of mark must agree; with
    neither known nothing is compared (no amend)."""
    prices = [x for x in (c.quote, c.mark) if x is not None]
    if not prices or not t.legs or t.side not in ("long", "short"):
        return False
    return all(x > max(t.legs) for x in prices) if t.side == "long" else all(x < min(t.legs) for x in prices)


def merge(roots: list[Item], supplements: list[Item] = (), *, conditionals: list[Item] = (), stage: int = 1,
          supplement_window_s: int = DEFAULT_SUPPLEMENT_S, rejected_supplements: dict[str, str] | None = None) -> tuple[dict[str, Link], dict[str, Any]]:
    """Causal merge in (t_vis, sequence, message_id, branch_index) order. Returns (links by pid, counters).

    ``rejected_supplements`` holds pids whose unit/scale gate already failed upstream (validate UNIT_SCALE_CONFLICT).
    ``conditionals`` are unpromoted conditional opens (and promoted roots that this scope cannot execute) usable by C0.
    """
    rejected_supplements = rejected_supplements or {}
    w_sup = int(supplement_window_s)
    items = {i.pid: i for i in list(roots) + list(supplements)}
    root_ids = {i.pid for i in roots}
    links: dict[str, Link] = {pid: Link(pid=pid) for pid in root_ids}
    groups: dict[str, Group] = {}
    by_key: dict[tuple, list[Group]] = {}
    by_message: dict[tuple, list[str]] = {}
    group_of: dict[str, str] = {}
    counters: dict[str, int] = {"ambiguous_targets": 0, "late_entry_dropped": 0, "late_stop_tps_dropped": 0, "supplement_rejected": 0,
                                "late_stop_without_price": 0, "frozen_stop_as_move": 0}
    #: group gid → the latest amend that replaced it (None: referenced by a repost only). A frozen group never absorbs.
    frozen: dict[str, str | None] = {}

    def freeze(g: Group, amender: str | None = None):
        if amender is not None or g.gid not in frozen:
            frozen[g.gid] = amender if amender is not None else frozen.get(g.gid)

    def stop_receiver(g: Group) -> Group:
        """Where a stop for a frozen group goes: the plan that replaced it (latest amend), else the group itself."""
        amender = frozen.get(g.gid)
        return groups[group_of[amender]] if amender is not None and amender in group_of else g

    def order(i: Item):
        return (i.t_vis, i.sequence if i.sequence is not None else -1, i.message_id, i.branch_index, i.pid)

    def new_group(c: Item, family: str | None = None) -> Group:
        g = Group(gid=c.pid, channel=c.channel, inst=c.inst, side=c.side, members=[c.pid], kept=c.pid, first_t=c.t_post or c.t_vis,
                  first_seq=c.sequence, entry_pid=c.pid, stop=c.stop, stop_pid=c.pid if c.stop is not None else None,
                  tps_pid=c.pid if c.tps else None, providers={c.pid}, family=family or c.pid, last_t=c.t_vis)
        groups[g.gid] = g
        group_of[c.pid] = g.gid
        by_key.setdefault((g.channel, g.inst, g.side), []).append(g)
        link = links[c.pid]
        link.group, link.kept, link.family = g.gid, c.pid, g.family
        return g

    def group_view(g: Group) -> Item:
        """The group's fields as of now: the kept entry with the group's stop."""
        base = items[g.entry_pid]
        return Item(pid=g.gid, svid=base.svid, channel=g.channel, message_id=base.message_id, t_vis=g.last_t, t_post=g.first_t,
                    author=base.author, inst=g.inst, side=g.side, legs=base.legs, market_ref=base.market_ref, mark=base.mark,
                    entry_kind=base.entry_kind, stop=g.stop)

    def member_messages(g: Group) -> set[int]:
        return {items[m].message_id for m in g.members}

    def choose_kept(cands: list[str]) -> str:
        """Entry provider: a card first, then an explicit price that also states the stop, then an explicit price, then the first."""
        def rank(pid):
            it = items[pid]
            return (0 if it.card and it.priced else 1 if it.priced and it.stop is not None else 2 if it.priced else 3, order(it))
        return min(cands, key=rank)

    def absorb(g: Group, c: Item, kind: str, *, stop_from_c: bool):
        g.members.append(c.pid)
        group_of[c.pid] = g.gid
        g.last_t = max(g.last_t, c.t_vis)
        if stop_from_c and c.stop is not None:
            g.stop, g.stop_pid = c.stop, c.pid
        g.kept = choose_kept([m for m in g.members if m in root_ids])
        g.entry_pid = g.kept
        cards = [m for m in g.members if m in root_ids and items[m].card and items[m].tps]
        with_tps = [m for m in g.members if m in root_ids and items[m].tps]
        g.tps_pid = (cards or ([g.kept] if items[g.kept].tps else with_tps) or [None])[0]
        # Only messages whose fields are used move t_dec: the entry (kept), the stop and the targets.
        g.providers = {p for p in (g.kept, g.stop_pid, g.tps_pid) if p is not None}
        g.kind = kind
        link = links[c.pid]
        link.kind, link.group, link.family = kind, g.gid, g.family

    def dup(c: Item, g: Group, kind: str, gap: float):
        g.members.append(c.pid)
        group_of[c.pid] = g.gid
        link = links[c.pid]
        link.kind, link.group, link.family, link.gap_s = kind, g.gid, g.family, _ceil_s(gap)
        link.target_message_id = items[g.kept].message_id

    def independent(c: Item, kind: str = "root", **extra):
        g = new_group(c)
        link = links[c.pid]
        link.kind = kind
        for k, v in extra.items():
            setattr(link, k, v)
        return g

    def repost(c: Item, t: Group, gap: float):
        # A repost is its own group in the target's family; G2 decides whether it runs (family alive at t_dec).
        new_group(c, family=t.family)
        freeze(t)
        link = links[c.pid]
        link.kind, link.repost_of = "repost", t.kept
        link.gap_s, link.target_message_id = _ceil_s(gap), items[t.kept].message_id

    plan_messages = {(i.channel, i.message_id) for i in roots}
    ordered = sorted(list(roots) + list(supplements), key=order)
    for c in ordered:
        # ---------------------------------------------------------------- S. stop supplement (F6)
        if c.supplement_of is not None:
            link = links.setdefault(c.pid, Link(pid=c.pid))
            link.supplement_of = c.supplement_of
            rid = c.supplement_of
            if rid not in group_of:
                link.supplement_rejected = "root_not_merged"
                continue
            g = groups[group_of[rid]]
            if g.stop is not None:
                link.kind = "stop_move"  # R already has a stop: a move, left to followup
                continue
            reason = rejected_supplements.get(c.pid) or supplement_check(c, group_view(g))
            if reason is not None:
                link.supplement_rejected = reason
                counters["supplement_rejected"] += 1
                continue
            # Δ is measured from the plan's original post (the group's first member), not from whichever member the
            # linker attached the stop to: a stop linked to a later restatement is still Δ after the plan was posted.
            gap = _gap(c, g)
            link.gap_s, link.target_message_id, link.group = _ceil_s(gap), items[g.members[0]].message_id, g.gid
            if g.gid in frozen:
                receiver = stop_receiver(g)
                if receiver.stop is not None:
                    link.kind = "stop_move"  # the amend that replaced R states its own stop: a move, left to followup
                    continue
                link.kind, link.kept = "late_stop", receiver.kept
                link.synthetic.append({"action": "move_stop", "target": receiver.kept, "at": c.t_vis, "stop": c.stop})
                counters["frozen_stop_as_move"] += int(gap <= w_sup)
                continue
            if gap <= w_sup:
                absorb(g, c, "supplement", stop_from_c=True)
                g.members.remove(c.pid)  # a supplement is not a member episode
                link.kind, link.kept = "supplement", g.kept
            else:
                link.kind, link.kept = "late_stop", g.kept
                link.synthetic.append({"action": "move_stop", "target": g.kept, "at": c.t_vis, "stop": c.stop})
            continue

        link = links[c.pid]
        # ---------------------------------------------------------------- C0. conditional confirmation
        if c.no_params:
            # A reply to an ordinary plan is about that plan (section B), never a confirmation of some other conditional
            # post by the same author: the same-author fallback only applies when C replies to no known plan message.
            replies_to_plan = c.reply_to is not None and (c.channel, c.reply_to) in plan_messages \
                and not any(p.channel == c.channel and p.message_id == c.reply_to for p in conditionals)
            parents = [p for p in conditionals if p.channel == c.channel and p.t_vis < c.t_vis
                       and (c.inst is None or p.inst == c.inst) and (c.side is None or p.side == c.side)
                       and (c.t_vis - (p.t_post or p.t_vis)).total_seconds() <= CONFIRM_S
                       and ((c.reply_to is not None and c.reply_to == p.message_id)
                            or (not replies_to_plan and c.author is not None and c.author == p.author
                                and (c.t_vis - (p.t_post or p.t_vis)).total_seconds() <= LINK_S))]
            replied = [p for p in parents if c.reply_to is not None and c.reply_to == p.message_id]
            pick = replied or parents
            if len({p.pid for p in pick}) == 1:
                p = pick[0]
                independent(c, "conditional_confirmed", conditional_parent=p.pid, target_message_id=p.message_id,
                            gap_s=_ceil_s((c.t_vis - (p.t_post or p.t_vis)).total_seconds()))
                continue
            if len(pick) > 1:
                counters["ambiguous_targets"] += 1

        # ---------------------------------------------------------------- A. branches of the same message
        same = [groups[group_of[pid]] for pid in by_message.get((c.channel, c.message_id), [])
                if pid in group_of and items[pid].inst == c.inst and items[pid].side == c.side and pid != c.pid]
        same = list({g.gid: g for g in same}.values())
        by_message.setdefault((c.channel, c.message_id), []).append(c.pid)
        if same:
            venue_groups = [g for g in same if any(venue_only_diff(c, items[m]) for m in g.members if m in root_ids)]
            stopped = [g for g in same if g.stop is not None and compatible(c, group_view(g))]
            if venue_groups:
                g = venue_groups[0]
                if items[g.kept].message_id != c.message_id:
                    # The sibling was already merged into an older message's plan: C follows the sibling there and the
                    # older kept plan (with its providers and stop source) is left as it is. Only branches of one message
                    # are re-ranked against each other.
                    sibling = next(m for m in g.members if m in root_ids and items[m].message_id == c.message_id)
                    kind = links[sibling].kind if links[sibling].kind in COMPANION_KINDS | RESTATEMENT_KINDS else "same_message_venue"
                    dup(c, g, kind, _gap(c, g))
                    continue
                contract = [m for m in g.members + [c.pid] if m in root_ids and items[m].venue == "unspecified"]
                g.members.append(c.pid)
                group_of[c.pid] = g.gid
                keep = min(contract or [m for m in g.members if m in root_ids], key=lambda pid: order(items[pid]))
                g.kept = g.entry_pid = keep
                for m in g.members:
                    if m in root_ids:
                        links[m].group, links[m].family = g.gid, g.family
                        if m != keep:
                            links[m].kind = "same_message_venue"
                links[keep].kind = "root" if links[keep].kind == "same_message_venue" else links[keep].kind
                continue
            if c.stop is None and not c.venue_words and stopped:
                g = stopped[0]
                dup(c, g, "same_message_split", 0)
                continue
            independent(c, "root")
            continue

        # ---------------------------------------------------------------- B. visible targets
        horizon = c.t_vis - timedelta(seconds=SAME_REPOST_S)
        visible = [g for g in by_key.get((c.channel, c.inst, c.side), []) if c.inst is not None
                   and (g.last_t >= horizon or (c.reply_to is not None and c.reply_to in member_messages(g)))
                   and all(_before(items[m], c) for m in g.members)]
        t_link, t_same = [], []
        for g in visible:
            gap = _gap(c, g)
            replied = c.reply_to is not None and c.reply_to in member_messages(g)
            same_author = c.author is not None and c.author == items[g.members[0]].author
            if gap < 0:
                continue
            if gap == 0 and not _before(g, c):
                continue
            view = group_view(g)
            if replied or (same_author and gap <= LINK_S) or (same_author and LINK_S < gap <= COMPATIBLE_LINK_S and compatible(c, view)):
                t_link.append(g)
            elif identical(c, view) and 0 < gap <= SAME_REPOST_S:
                t_same.append(g)
        rel = c.relation if stage >= 2 else None
        rel_target = None
        if rel in ("amends", "reenters") and c.relation_target is not None:
            rel_target = next((g for g in visible if c.relation_target in member_messages(g)), None)

        if len(t_link) > 1 and len({g.family for g in t_link}) == 1:
            t_link = [max(t_link, key=lambda g: (g.last_t, g.gid))]  # one repost family: its latest member
        if c.reentry_words or (rel == "reenters" and rel_target is not None):
            target = rel_target or (t_link[0] if len(t_link) == 1 else None)
            parent_stop = None
            if target is not None and target.stop is not None:
                legs = _cmp_legs(c)
                if not legs or correct_side(target.stop, legs, c.side):
                    parent_stop = target.stop
            independent(c, "reentry", reentry_of=target.kept if target else None,
                        target_message_id=items[target.kept].message_id if target else None, reentry_parent_stop=parent_stop)
            continue

        def amend(target: Group):
            g = independent(c, "amend", amend_of=target.kept, target_message_id=items[target.kept].message_id,
                            gap_s=_ceil_s(_gap(c, target)))
            freeze(target, c.pid)
            if c.stop is None and target.stop is not None:
                g.stop, g.stop_pid = target.stop, target.stop_pid
                links[c.pid].stop, links[c.pid].stop_pid = target.stop, target.stop_pid
            if not c.tps and target.tps_pid:
                g.tps_pid = target.tps_pid
                links[c.pid].tps_pid = target.tps_pid
            links[c.pid].synthetic.append({"action": "cancel_pending", "target": target.kept, "at": c.t_vis, "stop": None})

        if rel == "amends" and rel_target is not None:
            amend(rel_target)
            continue
        if len(t_link) == 1:
            t = t_link[0]
            view = group_view(t)
            replied = c.reply_to is not None and c.reply_to in member_messages(t)
            limit_target = items[t.entry_pid].priced and not items[t.entry_pid].market_ref
            if replied and c.market_ref and not c.no_params and c.stop is None and limit_target and _past_the_limit(c, items[t.entry_pid]):
                amend(t)
                continue
            if compatible(c, view):
                gap = _gap(c, t)
                paired = items[t.kept].has_media != c.has_media or items[t.kept].lang != c.lang
                adds_stop = c.stop is not None and t.stop is None
                adds_entry = c.priced and not view.priced
                if c.no_params:
                    if paired and gap <= COMPANION_S:
                        dup(c, t, "companion", gap)
                    elif replied or gap <= LINK_S:
                        dup(c, t, "restatement", gap)
                    else:
                        repost(c, t, gap)
                    continue
                if not adds_stop and not adds_entry:
                    if paired and gap <= COMPANION_S:
                        dup(c, t, "companion", gap)
                    elif gap <= LINK_S:
                        dup(c, t, "restatement", gap)
                    else:
                        repost(c, t, gap)
                    continue
                if adds_stop and t.gid in frozen:
                    receiver = stop_receiver(t)
                    dup(c, t, "late_stop", gap)
                    if receiver.stop is None:
                        links[c.pid].synthetic.append({"action": "move_stop", "target": receiver.kept, "at": c.t_vis, "stop": c.stop})
                        counters["frozen_stop_as_move"] += int(gap <= w_sup)
                    counters["late_entry_dropped"] += int(c.priced)
                    counters["late_stop_tps_dropped"] += int(bool(c.tps))
                    continue
                if adds_stop:
                    if gap <= w_sup:
                        if any(items[m].is_title for m in t.members) or c.is_title:
                            kind = "body_after_title"
                        elif items[t.kept].has_media != c.has_media:
                            kind = "image_text_pair"
                        elif c.priced:
                            kind = "commentary_formal"
                        else:
                            kind = "supplement"
                        absorb(t, c, kind, stop_from_c=True)
                        links[c.pid].gap_s = _ceil_s(gap)
                        links[c.pid].target_message_id = items[t.members[0]].message_id
                        continue
                    dup(c, t, "late_stop", gap)
                    links[c.pid].synthetic.append({"action": "move_stop", "target": t.kept, "at": c.t_vis, "stop": c.stop})
                    counters["late_entry_dropped"] += int(c.priced)
                    counters["late_stop_tps_dropped"] += int(bool(c.tps))
                    continue
                # only an explicit entry was added
                if gap <= COMPANION_S and t.gid not in frozen:
                    kind = "image_text_pair" if items[t.kept].has_media != c.has_media else "body_after_title"
                    absorb(t, c, kind, stop_from_c=False)
                    links[c.pid].gap_s = _ceil_s(gap)
                    links[c.pid].target_message_id = items[t.members[0]].message_id
                    continue
                if limit_target:
                    amend(t)
                    continue
                repost(c, t, gap)
                continue
            independent(c, "root")
            continue
        if len(t_link) == 0 and t_same:
            t = max(t_same, key=lambda g: g.last_t)
            repost(c, t, _gap(c, t))
            continue
        if len(t_link) > 1:
            counters["ambiguous_targets"] += 1
        independent(c, "root")

    # ---------------------------------------------------------------- D. finish: members point at the final kept
    for g in groups.values():
        for m in g.members:
            if m not in root_ids:
                continue
            link = links[m]
            link.kept = g.kept
            link.group = g.gid
            if m != g.kept and link.kind not in ("repost", "amend", "reentry", "conditional_confirmed"):
                link.dup_of = g.kept
                if link.kind == "root":
                    link.kind = g.kind
            if m == g.kept and link.kind in COMPANION_KINDS - {"same_message_split", "same_message_venue"}:
                link.kind = "root"  # the kept plan is the root of its group; members carry the merge kind
            if m == g.kept:
                link.stop, link.stop_pid = g.stop, g.stop_pid
                link.tps_pid = g.tps_pid
                link.providers = tuple(sorted(g.providers))
                if link.dup_of == m:
                    link.dup_of = None
    for link in links.values():
        if link.kind == "late_stop" and link.synthetic:
            for s in link.synthetic:
                s["target"] = groups[group_of[s["target"]]].kept if s["target"] in group_of else s["target"]
            if link.supplement_of is not None and link.supplement_of in group_of:
                link.kept = groups[group_of[link.supplement_of]].kept
        if link.repost_of is not None and link.repost_of in group_of:
            link.repost_of = groups[group_of[link.repost_of]].kept
        if link.amend_of is not None and link.amend_of in group_of:
            link.amend_of = groups[group_of[link.amend_of]].kept
            for s in link.synthetic:
                s["target"] = link.amend_of
        if link.reentry_of is not None and link.reentry_of in group_of:
            link.reentry_of = groups[group_of[link.reentry_of]].kept

    if stage >= 2:
        _flip_unexecutable(links, items, groups, group_of, root_ids)
    for link in links.values():
        if link.supplement_of is not None:
            continue
        counters[f"kind:{link.kind}"] = counters.get(f"kind:{link.kind}", 0) + 1
    return links, counters


def _flip_unexecutable(links, items, groups, group_of, root_ids):
    """Stage 2: a kept plan this scope cannot execute does not swallow restatements, late stops, reposts or companions."""
    for link in links.values():
        if link.pid not in root_ids or link.kind not in FLIP_KINDS:
            continue
        target = link.dup_of or link.repost_of
        if target is None or items[target].executable:
            continue
        link.kind, link.dup_of, link.repost_of = "root", None, None
        link.kept, link.group, link.family = link.pid, link.pid, link.pid
        link.synthetic = []
        it = items[link.pid]
        link.stop, link.stop_pid = it.stop, (it.pid if it.stop is not None else None)
        link.tps_pid = it.pid if it.tps else None
        link.providers = (it.pid,)
