"""原始导出读取器：TDesktop JSON（result.json + 媒体目录）与 Telethon 快照 jsonl。

只读；每个文件带 sha256（raw_hash）。字段缺失与明确空分开：缺失 → None，空串 → ""。
"""
from __future__ import annotations

import json
import pathlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Iterator

from .lake import sha256_file

CHANNEL_PEER_BASE = 1_000_000_000_000  # Telegram peer_id 规范：channel → -100xxxxxxxxxx
CHANNEL_CHAT_TYPES = frozenset({"public_channel", "private_channel"})

#: TDesktop 单条消息已知顶层字段（其余视为 SCHEMA_DRIFT 候选）
TDESKTOP_KNOWN_KEYS = frozenset(
    {
        "id", "type", "date", "date_unixtime", "edited", "edited_unixtime", "from", "from_id", "actor", "actor_id",
        "action", "title", "text", "text_entities", "reply_to_message_id", "reply_to_peer_id", "forwarded_from",
        "saved_from", "photo", "photo_file_size", "width", "height", "file", "file_name", "file_size", "thumbnail",
        "thumbnail_file_size", "media_type", "mime_type", "duration_seconds", "sticker_emoji", "views", "author",
        "inline_bot_buttons", "poll", "location_information", "contact_information", "members", "message_id",
        # 本项目扩展（合成夹具 / Telethon 补充导出会带）
        "grouped_id", "forwarded_from_id", "forwarded_from_message_id", "forwarded_date_unixtime",
        "first_seen_at", "snapshot_at",
    }
)


def canonical_peer_id(raw_id: int, kind: str | None) -> int:
    """TDesktop 的 id 是裸 id；频道/超群规范化为 -100 前缀形式，与 Telethon 一致。"""
    if raw_id < 0:
        return raw_id
    if kind and ("channel" in kind or "supergroup" in kind):
        return -(CHANNEL_PEER_BASE + raw_id)
    return raw_id


def canonical_channel_id(raw_id: int, chat_type: str | None) -> int:
    return canonical_peer_id(raw_id, chat_type)


def _skip_non_channel(chat_type: str | None) -> bool:
    return chat_type not in CHANNEL_CHAT_TYPES


def _whitelist_peer_id(raw_id: int) -> int:
    return canonical_peer_id(raw_id, "channel")


def load_channel_whitelist(path: pathlib.Path) -> frozenset[int] | None:
    """None = 无名单文件（不过滤）。空 frozenset = 启用但无 id（不摄入任何消息）。"""
    if not path.is_file():
        return None
    ids: list[int] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        ids.append(_whitelist_peer_id(int(s)))
    return frozenset(ids)


@dataclass
class MediaRef:
    kind: str  # photo | file | sticker | video ...
    rel_path: str  # 相对导出目录
    declared_size: int | None = None
    abs_path: pathlib.Path | None = None
    exists: bool = False
    sha256: str | None = None
    reject_reason: str | None = None  # absolute_path | parent_escape | symlink_escape | not_included | unresolvable


@dataclass
class RawMessage:
    channel_id: int
    channel_name: str
    message_id: int
    message_type: str
    date_unixtime: int | None
    date_raw: str | None
    edited_unixtime: int | None
    edited_raw: str | None
    text: str
    text_entities: list[dict[str, Any]]
    reply_to_message_id: int | None
    forwarded_from: str | None
    forwarded_from_id: int | None
    forwarded_from_message_id: int | None
    forwarded_date_unixtime: int | None
    grouped_id: int | None
    media: list[MediaRef]
    from_id: str | None
    action: str | None
    first_seen_at: datetime | None
    snapshot_at: datetime | None
    raw_uri: str
    raw_hash: str
    unknown_keys: list[str] = field(default_factory=list)
    time_unit_problem: str | None = None  # date_unixtime 缺失/不可解析的原因
    edit_time_problem: str | None = None  # edited_unixtime 存在但不可解析 → VERSION_TIME_UNKNOWN
    raw_index: int = 0  # 在导出文件中的顺序（同秒排序证据）
    export_snapshot_at: datetime | None = None  # 导出快照时刻（export_manifest.json.exported_at），H1 首次可证时刻
    cohort_id: str | None = None  # 实收观察 cohort 证据（jsonl 记录 cohort 字段）；无则 survival_scope=unknown

    sequence_evidence: int | None = None
    quote_only: bool = False

    @property
    def message_date(self) -> datetime | None:
        return _ts(self.date_unixtime)

    @property
    def last_edit_at(self) -> datetime | None:
        return _ts(self.edited_unixtime)


def _ts(unix: int | None) -> datetime | None:
    return None if unix is None else datetime.fromtimestamp(unix, tz=UTC)


def _parse_unix(v: Any) -> tuple[int | None, str | None]:
    """返回 (秒级 unix, 问题描述)。毫秒级（>1e11）视为单位不明。"""
    if v is None:
        return None, "missing"
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None, f"unparseable:{v!r}"
    if n <= 0:
        return None, f"nonpositive:{n}"
    if n > 100_000_000_000:
        return None, f"unit_ambiguous_ms?:{n}"
    return n, None


def flatten_text(t: Any) -> tuple[str, list[dict[str, Any]]]:
    """TDesktop text 可能是 str 或 [str | {type,text,...}] 混合列表；返回纯文本与实体（带 offset/length）。"""
    if t is None:
        return "", []
    if isinstance(t, str):
        return t, []
    parts: list[str] = []
    ents: list[dict[str, Any]] = []
    off = 0
    for p in t:
        if isinstance(p, str):
            parts.append(p)
            off += len(p)
        elif isinstance(p, dict):
            s = str(p.get("text", ""))
            e = {k: v for k, v in p.items() if k != "text"}
            e["offset"] = off
            e["length"] = len(s)
            ents.append(e)
            parts.append(s)
            off += len(s)
    return "".join(parts), ents


def safe_media_path(base: pathlib.Path, rel: str) -> tuple[pathlib.Path | None, str | None]:
    """媒体路径必须落在授权导出根内：拒绝绝对路径、`..` 逃逸（含 Windows 反斜杠分隔，TDesktop 导出常来自 Windows）与指向根外的符号链接（review S16）。
    返回 (真实路径, 拒绝原因)。不读、不 hash 任何根外文件。"""
    if not rel or rel.startswith("("):
        return None, "not_included"
    norm = rel.replace("\\", "/")  # 反斜杠归一后再判定，`..\x` 与 `../x` 同罪
    if pathlib.PurePosixPath(norm).is_absolute() or norm.startswith("/") or (len(norm) > 1 and norm[1] == ":") or norm.startswith("//"):
        return None, "absolute_path"
    if any(part == ".." for part in pathlib.PurePosixPath(norm).parts):
        return None, "parent_escape"
    root = base.resolve()
    p = base / norm
    try:
        real = p.resolve(strict=False)
    except (OSError, RuntimeError):
        return None, "unresolvable"
    if root != real and root not in real.parents:
        return None, "symlink_escape"
    return real, None


def _media_refs(m: dict[str, Any], base: pathlib.Path) -> list[MediaRef]:
    refs: list[MediaRef] = []
    for key, kind in (("photo", "photo"), ("file", m.get("media_type") or "file")):
        rel = m.get(key)
        if not rel:
            continue
        real, why = safe_media_path(base, rel)
        if real is None:
            refs.append(MediaRef(kind=kind, rel_path=rel, exists=False, reject_reason=why))
            continue
        ref = MediaRef(kind=kind, rel_path=rel, declared_size=m.get(f"{key}_file_size"), abs_path=real, exists=real.is_file())
        if ref.exists:
            ref.sha256 = sha256_file(real)
        refs.append(ref)
    return refs


def _dt(v: Any) -> datetime | None:
    if v in (None, ""):
        return None
    d = datetime.fromisoformat(str(v))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


@dataclass
class ChannelListing:
    """整号扫描得到的频道元信息（无消息正文）。"""

    channel_id: int
    name: str
    chat_type: str
    n_messages: int
    start: datetime | None
    end: datetime | None
    ingested: bool = True
    from_account: bool = False


@dataclass
class TDesktopScan:
    messages: list[RawMessage]
    skipped_by_type: dict[str, int]
    listings: list[ChannelListing]
    whole_account: bool


def _account_sections() -> tuple[tuple[str, bool], ...]:
    return (("chats", False), ("left_chats", True))


def is_whole_account_export(doc: Any) -> bool:
    """顶层含 chats 即为整号；值是否为 dict 在扫描时校验，不回退单聊天。"""
    if hasattr(doc, "__contains__"):
        return "chats" in doc
    sentinel = object()
    got = doc.get("chats", sentinel) if hasattr(doc, "get") else sentinel
    return got is not sentinel


def _section_chat_list(section: Any) -> list[Any]:
    if not isinstance(section, dict):
        raise TypeError("account_section_not_object")
    lst = section.get("list") if hasattr(section, "get") else None
    if lst is None:
        return []
    if not isinstance(lst, list):
        raise TypeError("account_section_list_invalid")
    return lst


def iter_account_chats(doc: Any) -> Iterator[tuple[Any, bool]]:
    """只读 chats / left_chats，不碰 personal_information / contacts。"""
    for key, left in _account_sections():
        if left and not (hasattr(doc, "__contains__") and key in doc):
            continue
        section = doc.get(key) if hasattr(doc, "get") else None
        for chat in _section_chat_list(section):
            yield chat, left


def _raw_from_tdesktop_item(
    m: dict[str, Any],
    idx: int,
    *,
    peer: int,
    name: str,
    raw_uri: str,
    raw_hash: str,
    snapshot_at: datetime | None,
    chat_dir: pathlib.Path,
) -> RawMessage:
    unix, prob = _parse_unix(m.get("date_unixtime"))
    eunix, eprob = _parse_unix(m.get("edited_unixtime")) if m.get("edited_unixtime") is not None else (None, None)
    funix, _ = _parse_unix(m.get("forwarded_date_unixtime")) if m.get("forwarded_date_unixtime") is not None else (None, None)
    text, ents = flatten_text(m.get("text"))
    if m.get("text_entities"):
        ents = list(m["text_entities"])
    return RawMessage(
        channel_id=peer,
        channel_name=name,
        message_id=int(m["id"]),
        message_type=str(m.get("type", "")),
        date_unixtime=unix,
        date_raw=m.get("date"),
        edited_unixtime=eunix,
        edited_raw=m.get("edited"),
        text=text,
        text_entities=ents,
        reply_to_message_id=m.get("reply_to_message_id"),
        forwarded_from=m.get("forwarded_from"),
        forwarded_from_id=m.get("forwarded_from_id"),
        forwarded_from_message_id=m.get("forwarded_from_message_id"),
        forwarded_date_unixtime=funix,
        grouped_id=m.get("grouped_id"),
        media=_media_refs(m, chat_dir),
        from_id=m.get("from_id") or m.get("actor_id"),
        action=m.get("action"),
        first_seen_at=_dt(m.get("first_seen_at")),
        snapshot_at=_dt(m.get("snapshot_at")),
        raw_uri=raw_uri,
        raw_hash=raw_hash,
        unknown_keys=sorted(set(m.keys()) - TDESKTOP_KNOWN_KEYS),
        time_unit_problem=prob,
        edit_time_problem=eprob,
        raw_index=idx,
        export_snapshot_at=snapshot_at,
    )


def _ingest_chat_messages(
    items: Any,
    *,
    peer: int,
    name: str,
    chat_type: str,
    chat_dir: pathlib.Path,
    raw_uri: str,
    raw_hash: str,
    snapshot_at: datetime | None,
    from_account: bool,
) -> tuple[list[RawMessage], ChannelListing]:
    messages: list[RawMessage] = []
    dates: list[datetime] = []
    n_ordinary = 0
    for idx, m in enumerate(items or []):
        rm = _raw_from_tdesktop_item(
            m, idx, peer=peer, name=name, raw_uri=raw_uri, raw_hash=raw_hash, snapshot_at=snapshot_at, chat_dir=chat_dir,
        )
        messages.append(rm)
        if rm.message_type == "message":
            n_ordinary += 1
        if rm.message_date is not None:
            dates.append(rm.message_date)
    listing = ChannelListing(
        channel_id=peer,
        name=name,
        chat_type=chat_type,
        n_messages=n_ordinary,
        start=min(dates) if dates else None,
        end=max(dates) if dates else None,
        ingested=True,
        from_account=from_account,
    )
    return messages, listing


def _present_listing(peer: int, chat_type: str, *, from_account: bool) -> ChannelListing:
    return ChannelListing(channel_id=peer, name="", chat_type=chat_type, n_messages=0, start=None, end=None, ingested=False, from_account=from_account)


def _scan_whole_account(
    doc: Any,
    *,
    chat_dir: pathlib.Path,
    raw_uri: str,
    raw_hash: str,
    snapshot_at: datetime | None,
    allowed_peer_ids: frozenset[int] | None,
) -> TDesktopScan:
    skipped: Counter[str] = Counter()
    listings: list[ChannelListing] = []
    messages: list[RawMessage] = []
    for chat, _left in iter_account_chats(doc):
        chat_type = chat.get("type") if hasattr(chat, "get") else None
        type_key = chat_type if isinstance(chat_type, str) else None
        if _skip_non_channel(type_key):
            skipped[type_key or "unknown"] += 1
            continue
        peer = canonical_channel_id(int(chat["id"]), type_key)
        ingest = allowed_peer_ids is None or peer in allowed_peer_ids
        if not ingest:
            listings.append(_present_listing(peer, type_key or "", from_account=True))
            continue
        name = chat.get("name") or str(chat["id"])
        chunk, listing = _ingest_chat_messages(
            chat.get("messages"),
            peer=peer,
            name=name,
            chat_type=type_key or "",
            chat_dir=chat_dir,
            raw_uri=raw_uri,
            raw_hash=raw_hash,
            snapshot_at=snapshot_at,
            from_account=True,
        )
        messages.extend(chunk)
        listings.append(listing)
    return TDesktopScan(messages, dict(skipped), listings, True)


def _scan_single_chat(
    doc: Any,
    *,
    chat_dir: pathlib.Path,
    raw_uri: str,
    raw_hash: str,
    snapshot_at: datetime | None,
    allowed_peer_ids: frozenset[int] | None,
) -> TDesktopScan:
    kind = doc.get("type")
    peer = canonical_peer_id(int(doc["id"]), kind)
    name = doc.get("name") or str(doc["id"])
    type_key = kind if isinstance(kind, str) else None
    is_channel = type_key is not None and not _skip_non_channel(type_key)
    if allowed_peer_ids is not None and (not is_channel or peer not in allowed_peer_ids):
        listings = [_present_listing(peer, type_key or "", from_account=False)] if is_channel else []
        return TDesktopScan([], {}, listings, False)
    chunk, listing = _ingest_chat_messages(
        doc.get("messages"),
        peer=peer,
        name=name,
        chat_type=type_key or "",
        chat_dir=chat_dir,
        raw_uri=raw_uri,
        raw_hash=raw_hash,
        snapshot_at=snapshot_at,
        from_account=False,
    )
    return TDesktopScan(chunk, {}, [listing], False)


def scan_tdesktop_document(
    doc: Any,
    chat_dir: pathlib.Path,
    *,
    raw_uri: str,
    raw_hash: str,
    snapshot_at: datetime | None = None,
    allowed_peer_ids: frozenset[int] | None = None,
) -> TDesktopScan:
    """过滤+收集：整号只入频道；单聊天顶层 name/type/id/messages 默认行为不变。"""
    if is_whole_account_export(doc):
        return _scan_whole_account(
            doc, chat_dir=chat_dir, raw_uri=raw_uri, raw_hash=raw_hash, snapshot_at=snapshot_at, allowed_peer_ids=allowed_peer_ids,
        )
    return _scan_single_chat(
        doc, chat_dir=chat_dir, raw_uri=raw_uri, raw_hash=raw_hash, snapshot_at=snapshot_at, allowed_peer_ids=allowed_peer_ids,
    )


def scan_tdesktop_export(
    chat_dir: pathlib.Path,
    *,
    root: pathlib.Path | None = None,
    allowed_peer_ids: frozenset[int] | None = None,
) -> TDesktopScan:
    """读取一个 TDesktop 导出目录（含 result.json）。"""
    result = chat_dir / "result.json"
    raw_hash = sha256_file(result)
    root = root or chat_dir.parent
    raw_uri = str(result.relative_to(root))
    with open(result, encoding="utf-8") as f:
        doc = json.load(f)
    snapshot_at = None
    mf = chat_dir / "export_manifest.json"
    if mf.is_file():
        try:
            snapshot_at = _dt(json.loads(mf.read_text(encoding="utf-8")).get("exported_at"))
        except (ValueError, TypeError):
            snapshot_at = None
    return scan_tdesktop_document(
        doc, chat_dir, raw_uri=raw_uri, raw_hash=raw_hash, snapshot_at=snapshot_at, allowed_peer_ids=allowed_peer_ids,
    )


def read_tdesktop_export(
    chat_dir: pathlib.Path,
    *,
    root: pathlib.Path | None = None,
    allowed_peer_ids: frozenset[int] | None = None,
) -> Iterator[RawMessage]:
    yield from scan_tdesktop_export(chat_dir, root=root, allowed_peer_ids=allowed_peer_ids).messages


def ingest_tdesktop_dir(
    fixture_dir: pathlib.Path,
    *,
    allowed_peer_ids: frozenset[int] | None = None,
) -> TDesktopScan:
    messages: list[RawMessage] = []
    skipped: Counter[str] = Counter()
    listings: list[ChannelListing] = []
    whole = False
    for kind, p in discover(fixture_dir, tdesktop_only=True):
        scan = scan_tdesktop_export(p, root=fixture_dir, allowed_peer_ids=allowed_peer_ids)
        messages.extend(scan.messages)
        skipped.update(scan.skipped_by_type)
        listings.extend(scan.listings)
        whole = whole or scan.whole_account
    return TDesktopScan(messages, dict(sorted(skipped.items())), listings, whole)


def read_telethon_jsonl(path: pathlib.Path, *, root: pathlib.Path | None = None) -> Iterator[RawMessage]:
    """Telethon 实收快照（每行一条观察，可对同一消息多次观察 → 多版本）。字段：
    peer_id, id, date(unix), edit_date(unix)?, message, entities?, reply_to_msg_id?, grouped_id?,
    fwd_from{from_name?, from_id?, channel_post?, date?}?, media[{kind, path, size}]?, first_seen_at, snapshot_at?
    """
    raw_hash = sha256_file(path)
    root = root or path.parent
    raw_uri = str(path.relative_to(root))
    with open(path, encoding="utf-8") as f:
        for idx, line in enumerate(f):
            if not line.strip():
                continue
            m = json.loads(line)
            unix, prob = _parse_unix(m.get("date"))
            eunix, eprob = _parse_unix(m.get("edit_date")) if m.get("edit_date") is not None else (None, None)
            fwd = m.get("fwd_from") or {}
            fdate, _ = _parse_unix(fwd.get("date")) if fwd.get("date") is not None else (None, None)
            media = []
            for md in m.get("media") or []:
                real, why = safe_media_path(path.parent, md["path"])
                if real is None:
                    media.append(MediaRef(kind=md.get("kind", "photo"), rel_path=md["path"], exists=False, reject_reason=why))
                    continue
                ref = MediaRef(kind=md.get("kind", "photo"), rel_path=md["path"], declared_size=md.get("size"), abs_path=real, exists=real.is_file())
                if ref.exists:
                    ref.sha256 = sha256_file(real)
                media.append(ref)
            yield RawMessage(
                channel_id=int(m["peer_id"]),
                channel_name=m.get("peer_name") or str(m["peer_id"]),
                message_id=int(m["id"]),
                message_type="service" if m.get("action") else "message",
                date_unixtime=unix,
                date_raw=str(m.get("date")),
                edited_unixtime=eunix,
                edited_raw=str(m.get("edit_date")) if m.get("edit_date") is not None else None,
                text=m.get("message") or "",
                text_entities=list(m.get("entities") or []),
                reply_to_message_id=m.get("reply_to_msg_id"),
                forwarded_from=fwd.get("from_name"),
                forwarded_from_id=fwd.get("from_id"),
                forwarded_from_message_id=fwd.get("channel_post"),
                forwarded_date_unixtime=fdate,
                grouped_id=m.get("grouped_id"),
                media=media,
                from_id=str(m.get("from_id")) if m.get("from_id") is not None else None,
                action=m.get("action"),
                first_seen_at=_dt(m.get("first_seen_at")),
                snapshot_at=_dt(m.get("snapshot_at")),
                raw_uri=raw_uri,
                raw_hash=raw_hash,
                unknown_keys=[],
                time_unit_problem=prob,
                edit_time_problem=eprob,
                raw_index=idx,
                cohort_id=m.get("cohort"),
                sequence_evidence=m.get("observed_sequence"),
                quote_only=m.get("quote_only", False),
            )


def discover(fixture_dir: pathlib.Path, *, tdesktop_only: bool = False) -> list[tuple[str, pathlib.Path]]:
    """枚举导出：每个含 result.json 的子目录（tdesktop）与 *.jsonl（telethon）。"""
    items: list[tuple[str, pathlib.Path]] = []
    for p in sorted(fixture_dir.rglob("result.json")):
        items.append(("tdesktop", p.parent))
    if not tdesktop_only:
        for p in sorted(fixture_dir.rglob("*.jsonl")):
            items.append(("telethon", p))
    return items


def read_all(
    fixture_dir: pathlib.Path,
    *,
    tdesktop_only: bool = False,
    allowed_peer_ids: frozenset[int] | None = None,
) -> Iterator[RawMessage]:
    for kind, p in discover(fixture_dir, tdesktop_only=tdesktop_only):
        if kind == "tdesktop":
            yield from read_tdesktop_export(p, root=fixture_dir, allowed_peer_ids=allowed_peer_ids)
        else:
            yield from read_telethon_jsonl(p, root=fixture_dir)
