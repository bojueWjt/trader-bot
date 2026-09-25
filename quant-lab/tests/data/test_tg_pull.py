"""Telegram history-pull：离线假客户端、JSONL 断点与 harvest 跨源。不连网、不登录。"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import pathlib
import shutil
import textwrap
import warnings
from datetime import UTC, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from quant_lab.data import harvest, sources, tg_pull
from quant_lab.data.lake import Layout
from quant_lab.data.tg_pull import (
    ABORT_CODES,
    ALLOWED_RPC,
    APP_VERSION,
    AUTHORIZED_PEERS,
    FLOOD_WAIT_HINT,
    JSONL_KEYS,
    PullAbort,
    PullRefusal,
    PullState,
    RpcGuard,
    TelethonTransport,
    honest_identity,
    load_state,
    pull_history,
    rpc_allowed,
    rpc_qualname,
    serialize_message,
)

PEER_A, PEER_B = sorted(AUTHORIZED_PEERS)
STAMP = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
STAMP2 = datetime(2026, 9, 25, 8, 0, tzinfo=UTC)
UNIX = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)
PHONE = "+19995550123"
DIGEST = "deadbeefcafebabe"
CODE = "11111"


def mutant(function, before, after):
    source = textwrap.dedent(inspect.getsource(function))
    assert source.count(before) == 1
    namespace = dict(function.__globals__)
    exec(compile(source.replace(before, after), "<tg-pull-mutant>", "exec"), namespace)
    return namespace[function.__name__]


def assert_kills(invariant, function, before, after, install, uninstall):
    invariant()
    broken = mutant(function, before, after)
    install(broken)
    try:
        with pytest.raises(AssertionError):
            invariant()
    finally:
        uninstall()


class FloodWaitError(Exception):
    def __init__(self, seconds: int):
        super().__init__(seconds)
        self.seconds = seconds


def make_get_file_request():
    from telethon.tl.functions.upload import GetFileRequest
    from telethon.tl.types import InputPhotoFileLocation

    return GetFileRequest(
        location=InputPhotoFileLocation(id=0, access_hash=0, file_reference=b"\x00", thumb_size=""),
        offset=0,
        limit=4096,
    )


class Ent:
    def to_dict(self):
        return {"_": "MessageEntityBold", "offset": 0, "length": 2}


class Fwd:
    from_name = "fwd-name"
    from_id = SimpleNamespace(channel_id=2136478186)
    channel_post = 8
    date = datetime(2024, 2, 1, tzinfo=UTC)


class Act:
    pass


class FakeMsg:
    def __init__(self, mid: int, *, text: str = "hi", photo: bool = False, **extra):
        self.id = mid
        self.date = datetime(2024, 1, 1, tzinfo=UTC)
        self.message = text
        self.photo = object() if photo else None
        self.media = None
        self.edit_date = extra.get("edit_date")
        self.entities = extra.get("entities") or []
        self.reply_to_msg_id = extra.get("reply_to_msg_id")
        self.grouped_id = extra.get("grouped_id")
        self.fwd_from = extra.get("fwd_from")
        self.from_id = extra.get("from_id")
        self.action = extra.get("action")


class FakeTransport:
    def __init__(
        self,
        inbox: dict[int, list[FakeMsg]],
        *,
        chunks: int = 1,
        rename: bool = False,
        fail_photo: bool = False,
        floods: list[int] | None = None,
        always_flood: int | None = None,
        missing_entity: bool = False,
    ):
        self.inbox = inbox
        self.chunks = chunks
        self.rename = rename
        self.fail_photo = fail_photo
        self.floods = list(floods or [])
        self.always_flood = always_flood
        self.calls: list[str] = []
        self.file_calls = 0
        self.dialogs = 0
        self.missing_entity = missing_entity
        self.entities = {pid: pid for pid in inbox}

    def __getattr__(self, name):
        if name.startswith("send_") or name in {"join", "join_chat", "send_read_acknowledge"}:
            def _boom(*_a, **_k):
                raise PullRefusal("RPC_NOT_ALLOWED")

            return _boom
        raise AttributeError(name)

    async def send_message(self, *_a, **_k):
        raise PullRefusal("RPC_NOT_ALLOWED")

    async def get_input_peer(self, peer_id: int):
        if self.missing_entity and peer_id not in self.entities:
            raise ValueError("Could not find the input entity")
        return peer_id

    async def invoke(self, request):
        self.calls.append(rpc_qualname(request))
        if self.always_flood is not None:
            raise FloodWaitError(self.always_flood)
        if self.floods:
            raise FloodWaitError(self.floods.pop(0))
        name = type(request).__name__
        if name == "GetFileRequest":
            self.file_calls += 1
            return object()
        if name == "GetDialogsRequest":
            self.dialogs += 1
            for pid in list(self.inbox):
                self.entities[pid] = pid
            return SimpleNamespace(dialogs=[], chats=[])
        if name != "GetHistoryRequest":
            raise AssertionError(name)
        peer = request.peer if isinstance(request.peer, int) else PEER_A
        floor = (request.offset_id - 1) if getattr(request, "offset_id", 0) else 0
        msgs = [m for m in self.inbox.get(peer, []) if m.id > floor]
        msgs.sort(key=lambda m: m.id)
        return SimpleNamespace(messages=msgs[: request.limit], chats=[])

    async def download_photo(self, message, dest: pathlib.Path):
        for _ in range(self.chunks):
            await self.invoke(make_get_file_request())
        if self.fail_photo:
            return None
        actual = dest.with_suffix(".png") if self.rename else dest
        actual.parent.mkdir(parents=True, exist_ok=True)
        actual.write_bytes(PNG)
        return actual


class FakeSender:
    def __init__(self, *, floods: list[int] | None = None):
        self.calls: list[str] = []
        self.floods = list(floods or [])
        self._user_connected = True

    def send(self, request, ordered=False):
        loop = asyncio.get_running_loop()
        items = list(request) if isinstance(request, (list, tuple)) else [request]
        futs = []
        for item in items:
            self.calls.append(type(item).__name__)
            fut = loop.create_future()
            if self.floods:
                fut.set_exception(FloodWaitError(self.floods.pop(0)))
            else:
                fut.set_result(SimpleNamespace(ok=True, messages=[], chats=[]))
            futs.append(fut)
        return futs if isinstance(request, (list, tuple)) else futs[0]


def _sleep_log():
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    return sleep, slept


def _run(out: pathlib.Path, transport: FakeTransport, **kw):
    sleep, slept = _sleep_log()
    kw.setdefault("peers", [PEER_A])
    kw.setdefault("media", "none")
    kw.setdefault("wait", 1.0)
    kw.setdefault("now", STAMP)
    state = asyncio.run(pull_history(out_dir=out, transport=transport, sleep=sleep, **kw))
    return state, slept


def _rows(path: pathlib.Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def jsonl_row(*, peer_id: int, mid: int, text: str, date: int = UNIX, snapshot: str | None = None, **extra):
    rec = {key: None for key in JSONL_KEYS}
    rec.update(
        {
            "peer_id": peer_id,
            "peer_name": str(peer_id),
            "id": mid,
            "date": date,
            "message": text,
            "first_seen_at": None,
            "snapshot_at": snapshot or STAMP.isoformat(),
            "cohort": extra.pop("cohort", "history-pull-2026-09-24"),
            "edit_date": extra.pop("edit_date", None),
            "entities": [],
            "reply_to_msg_id": None,
            "grouped_id": None,
            "fwd_from": None,
            "media": extra.pop("media", []),
            "from_id": None,
            "action": None,
        }
    )
    rec.update(extra)
    return rec


def write_jsonl(path: pathlib.Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def test_rpc_allowed_types_wrappers_and_writes():
    hist = type("GetHistoryRequest", (), {"__module__": "telethon.tl.functions.messages"})()
    msgs = type("GetMessagesRequest", (), {"__module__": "telethon.tl.functions.messages"})()
    signup = type("SignUpRequest", (), {"__module__": "telethon.tl.functions.auth"})()
    send = type("SendMessageRequest", (), {"__module__": "telethon.tl.functions.messages"})()
    other_state = type("GetStateRequest", (), {"__module__": "telethon.tl.functions.account"})()
    ok_state = type("GetStateRequest", (), {"__module__": "telethon.tl.functions.updates"})()
    wrap = type("InvokeWithLayerRequest", (), {"__module__": "telethon.tl.functions"})()
    wrap.query = send
    ok_wrap = type("InvokeWithLayerRequest", (), {"__module__": "telethon.tl.functions"})()
    ok_wrap.query = hist
    assert rpc_allowed(hist) and rpc_allowed(ok_state) and rpc_allowed(ok_wrap)
    assert not rpc_allowed(msgs) and not rpc_allowed(signup) and not rpc_allowed(send)
    assert not rpc_allowed(other_state) and not rpc_allowed(wrap)
    assert not rpc_allowed("GetHistoryRequest")
    assert rpc_allowed("telethon.tl.functions.messages.GetHistoryRequest")
    assert "telethon.tl.functions.auth.SignUpRequest" not in ALLOWED_RPC
    assert "telethon.tl.functions.messages.GetDialogsRequest" in ALLOWED_RPC
    assert "telethon.tl.functions.updates.GetDifferenceRequest" in ALLOWED_RPC


def test_write_rpc_through_guard_throws(tmp_path):
    state = PullState(out_dir=tmp_path)
    send = type("SendMessageRequest", (), {"__module__": "telethon.tl.functions.messages"})()

    async def inner(request):
        return "sent"

    guard = RpcGuard(inner, state, sleep=_sleep_log()[0], cap=10)
    with pytest.raises(PullRefusal, match="RPC_NOT_ALLOWED"):
        asyncio.run(guard.invoke(send))
    assert state.rpc_count == 0


def test_fields_roundtrip_first_seen_null_and_cohort(tmp_path):
    out = tmp_path / "out"
    msg = FakeMsg(
        7,
        text="body",
        edit_date=datetime(2024, 1, 2, tzinfo=UTC),
        reply_to_msg_id=3,
        grouped_id=99,
        from_id=PEER_B,
        action=Act(),
        entities=[Ent()],
        fwd_from=Fwd(),
        photo=True,
    )
    transport = FakeTransport({PEER_A: [msg]}, rename=True)
    _run(out, transport, now=STAMP, media="photos")
    rows = _rows(tg_pull.jsonl_path(out, PEER_A))
    assert len(rows) == 1
    rec = rows[0]
    assert set(rec) >= set(JSONL_KEYS)
    assert rec["first_seen_at"] is None
    assert rec["cohort"] == "history-pull-2026-09-24"
    assert rec["snapshot_at"] == STAMP.isoformat()
    assert rec["id"] == 7 and rec["message"] == "body" and rec["peer_id"] == PEER_A
    assert rec["from_id"] == PEER_B
    assert rec["action"] == "Act"
    assert rec["entities"][0]["_"] == "MessageEntityBold"
    assert rec["fwd_from"]["from_name"] == "fwd-name"
    assert rec["media"][0]["path"].endswith(".png")
    parsed = list(sources.read_telethon_jsonl(tg_pull.jsonl_path(out, PEER_A), root=out))
    assert parsed[0].first_seen_at is None
    assert parsed[0].cohort_id == "history-pull-2026-09-24"
    assert parsed[0].export_snapshot_at == STAMP
    assert parsed[0].message_id == 7
    assert parsed[0].media[0].exists


def test_batch_limit_and_wait(tmp_path):
    out = tmp_path / "out"
    transport = FakeTransport({PEER_A: [FakeMsg(1), FakeMsg(2), FakeMsg(3)]})
    state, slept = _run(out, transport, batch=1, wait=1.5, now=STAMP)
    assert [r["id"] for r in _rows(tg_pull.jsonl_path(out, PEER_A))] == [1, 2, 3]
    assert slept.count(1.5) >= 3
    assert state.rpc_count >= 4
    with pytest.raises(PullRefusal, match="BATCH_INVALID"):
        _run(tmp_path / "b0", FakeTransport({PEER_A: []}), batch=0)
    with pytest.raises(PullRefusal, match="BATCH_INVALID"):
        _run(tmp_path / "b101", FakeTransport({PEER_A: []}), batch=101)
    with pytest.raises(PullRefusal, match="WAIT_TOO_SMALL"):
        _run(tmp_path / "w", FakeTransport({PEER_A: []}), wait=0.5)
    with pytest.raises(PullRefusal, match="WAIT_TOO_SMALL"):
        _run(tmp_path / "inf", FakeTransport({PEER_A: []}), wait=float("inf"))


def test_repeated_flood_then_hard_limit(tmp_path):
    out = tmp_path / "out"
    transport = FakeTransport({PEER_A: [FakeMsg(1)]}, floods=[2, 2])
    state, slept = _run(out, transport, wait=1.0, now=STAMP)
    assert [r["id"] for r in _rows(tg_pull.jsonl_path(out, PEER_A))] == [1]
    assert slept[:2] == [pytest.approx(2.2), pytest.approx(2.2)]
    assert state.rpc_count >= 3

    out2 = tmp_path / "hard"
    transport2 = FakeTransport({PEER_A: [FakeMsg(1)]}, floods=[3601])
    with pytest.raises(PullAbort, match="FLOOD_WAIT"):
        _run(out2, transport2)
    assert _rows(tg_pull.jsonl_path(out2, PEER_A)) == []

    out3 = tmp_path / "hour"
    transport3 = FakeTransport({PEER_A: [FakeMsg(1)]}, floods=[3600])
    _run(out3, transport3)
    assert [r["id"] for r in _rows(tg_pull.jsonl_path(out3, PEER_A))] == [1]


def test_flood_loop_until_cap_and_persist(tmp_path):
    out = tmp_path / "out"
    transport = FakeTransport({PEER_A: [FakeMsg(1)]}, always_flood=1)
    with pytest.raises(PullAbort, match="RPC_CAP"):
        _run(out, transport, cap=4, wait=1.0)
    raw = json.loads((out / tg_pull.STATE_NAME).read_text(encoding="utf-8"))
    assert raw["rpc_count"] == 4


def test_photo_chunks_rename_fail_and_media_none(tmp_path):
    out = tmp_path / "photos"
    transport = FakeTransport({PEER_A: [FakeMsg(1, photo=True)]}, chunks=3, rename=True)
    state, slept = _run(out, transport, media="photos", media_wait=1.5, wait=1.0, cap=20)
    rec = _rows(tg_pull.jsonl_path(out, PEER_A))[0]
    assert rec["media"][0]["path"].endswith(".png")
    assert rec["media"][0]["size"] == len(PNG)
    assert (out / rec["media"][0]["path"]).is_file()
    assert transport.file_calls == 3
    assert 1.5 in slept
    assert rec["first_seen_at"] is None

    fail_dir = tmp_path / "fail"
    with pytest.raises(PullAbort, match="PHOTO_FAILED"):
        _run(fail_dir, FakeTransport({PEER_A: [FakeMsg(1, photo=True)]}, fail_photo=True), media="photos")
    assert _rows(tg_pull.jsonl_path(fail_dir, PEER_A)) == []
    assert load_state(fail_dir, [PEER_A], now=STAMP).last_ids[PEER_A] == 0

    none_dir = tmp_path / "none"
    t_none = FakeTransport({PEER_A: [FakeMsg(1, photo=True)]}, chunks=2)
    _run(none_dir, t_none, media="none")
    assert t_none.file_calls == 0
    assert _rows(tg_pull.jsonl_path(none_dir, PEER_A))[0]["media"] == []

    cap_dir = tmp_path / "cap"
    t_cap = FakeTransport({PEER_A: [FakeMsg(1, photo=True)]}, chunks=3)
    with pytest.raises(PullAbort, match="RPC_CAP"):
        _run(cap_dir, t_cap, media="photos", cap=3)
    assert _rows(tg_pull.jsonl_path(cap_dir, PEER_A)) == []
    assert state.rpc_count > 0


def test_rpc_count_reset_on_rerun_and_mutant(tmp_path):
    out = tmp_path / "out"
    transport = FakeTransport({PEER_A: [FakeMsg(1), FakeMsg(2)]})
    with pytest.raises(PullAbort, match="RPC_CAP"):
        _run(out, transport, cap=1, wait=1.0, batch=1)
    assert json.loads((out / tg_pull.STATE_NAME).read_text())["rpc_count"] == 1
    transport2 = FakeTransport({PEER_A: [FakeMsg(1), FakeMsg(2)]})
    state = _run(out, transport2, cap=5, now=STAMP2, batch=1)[0]
    assert [r["id"] for r in _rows(tg_pull.jsonl_path(out, PEER_A))] == [1, 2]
    assert state.rpc_count >= 1

    broken = mutant(tg_pull.load_state, "rpc_count = 0  # 每次运行重置", "rpc_count = int(raw.get('rpc_count') or 0)  # 每次运行重置")
    out_m = tmp_path / "mut"
    with pytest.raises(PullAbort, match="RPC_CAP"):
        _run(out_m, FakeTransport({PEER_A: [FakeMsg(1), FakeMsg(2)]}), cap=1, batch=1)
    tg_pull.load_state = broken
    try:
        with pytest.raises(PullAbort, match="RPC_CAP"):
            _run(out_m, FakeTransport({PEER_A: [FakeMsg(1), FakeMsg(2)]}), cap=1, now=STAMP2, batch=1)
        assert [r["id"] for r in _rows(tg_pull.jsonl_path(out_m, PEER_A))] == [1]
    finally:
        tg_pull.load_state = load_state


def test_snapshot_not_reused_and_mutant(tmp_path):
    out = tmp_path / "out"
    _run(out, FakeTransport({PEER_A: [FakeMsg(1)]}), now=STAMP)
    (out / tg_pull.STATE_NAME).write_text(json.dumps({"rpc_count": 9, "snapshot_at": STAMP.isoformat(), "cohort": "history-pull-1999-01-01"}))
    _run(out, FakeTransport({PEER_A: [FakeMsg(1), FakeMsg(2)]}), now=STAMP2)
    stamps = {r["snapshot_at"] for r in _rows(tg_pull.jsonl_path(out, PEER_A))}
    assert STAMP.isoformat() in stamps and STAMP2.isoformat() in stamps
    assert all(r["cohort"] != "history-pull-1999-01-01" for r in _rows(tg_pull.jsonl_path(out, PEER_A)))

    broken = mutant(
        tg_pull.pull_peer,
        "stamp = now()  # 每条实际拉取时刻，不用旧 snapshot",
        "stamp = state.snapshot_at or now()  # 每条实际拉取时刻，不用旧 snapshot",
    )
    load_broken = mutant(
        tg_pull.load_state,
        "snapshot = now.astimezone(UTC).replace(microsecond=0)  # 本轮实际拉取时刻，不用旧文件",
        "snapshot = datetime.fromisoformat(str(raw.get('snapshot_at') or now.isoformat()))  # 本轮实际拉取时刻，不用旧文件",
    )
    out_m = tmp_path / "mut-snap"
    _run(out_m, FakeTransport({PEER_A: [FakeMsg(1)]}), now=STAMP)
    (out_m / tg_pull.STATE_NAME).write_text(json.dumps({"rpc_count": 0, "snapshot_at": STAMP.isoformat()}))
    orig_load, orig_peer = tg_pull.load_state, tg_pull.pull_peer
    tg_pull.load_state = load_broken
    tg_pull.pull_peer = broken
    try:
        asyncio.run(
            tg_pull.pull_history(
                peers=[PEER_A],
                out_dir=out_m,
                media="none",
                wait=1.0,
                transport=FakeTransport({PEER_A: [FakeMsg(1), FakeMsg(2)]}),
                sleep=_sleep_log()[0],
                now=STAMP2,
            )
        )
        later = [r for r in _rows(tg_pull.jsonl_path(out_m, PEER_A)) if r["id"] == 2]
        with pytest.raises(AssertionError):
            assert later[0]["snapshot_at"] == STAMP2.isoformat()
    finally:
        tg_pull.load_state = orig_load
        tg_pull.pull_peer = orig_peer


def test_resume_no_duplicates_including_completed_run_new_msgs(tmp_path):
    out = tmp_path / "out"
    _run(out, FakeTransport({PEER_A: [FakeMsg(1), FakeMsg(2)]}), now=STAMP)
    ckpt = json.loads(tg_pull.ckpt_path(out, PEER_A).read_text())
    assert ckpt["done"] is True
    _run(out, FakeTransport({PEER_A: [FakeMsg(1), FakeMsg(2), FakeMsg(3)]}), now=STAMP2)
    ids = [r["id"] for r in _rows(tg_pull.jsonl_path(out, PEER_A))]
    assert ids == [1, 2, 3]

    before = "seen = existing_jsonl_ids(jsonl_path(out_dir, peer_id))  # 不因上次 done 跳过新消息"
    after = "if state.done.get(peer_id):\n                    continue\n                seen = existing_jsonl_ids(jsonl_path(out_dir, peer_id))  # 不因上次 done 跳过新消息"
    broken = mutant(tg_pull.pull_history, before, after)
    out_m = tmp_path / "done-mut"
    asyncio.run(
        pull_history(
            peers=[PEER_A],
            out_dir=out_m,
            media="none",
            wait=1.0,
            transport=FakeTransport({PEER_A: [FakeMsg(1)]}),
            sleep=_sleep_log()[0],
            now=STAMP,
        )
    )
    orig = tg_pull.pull_history
    tg_pull.pull_history = broken
    try:
        asyncio.run(
            broken(
                peers=[PEER_A],
                out_dir=out_m,
                media="none",
                wait=1.0,
                transport=FakeTransport({PEER_A: [FakeMsg(1), FakeMsg(2)]}),
                sleep=_sleep_log()[0],
                now=STAMP2,
            )
        )
        with pytest.raises(AssertionError):
            assert [r["id"] for r in _rows(tg_pull.jsonl_path(out_m, PEER_A))] == [1, 2]
    finally:
        tg_pull.pull_history = orig


def test_stale_checkpoint_and_half_tail(tmp_path):
    out = tmp_path / "out"
    path = tg_pull.jsonl_path(out, PEER_A)
    write_jsonl(path, [jsonl_row(peer_id=PEER_A, mid=1, text="a")])
    tg_pull.ckpt_path(out, PEER_A).parent.mkdir(parents=True, exist_ok=True)
    tg_pull.ckpt_path(out, PEER_A).write_text(json.dumps({"peer_id": PEER_A, "last_id": 99, "done": True}))
    _run(out, FakeTransport({PEER_A: [FakeMsg(1, text="a"), FakeMsg(2, text="b")]}), now=STAMP2)
    assert [r["id"] for r in _rows(path)] == [1, 2]

    tail = tmp_path / "tail"
    tpath = tg_pull.jsonl_path(tail, PEER_A)
    write_jsonl(tpath, [jsonl_row(peer_id=PEER_A, mid=1, text="a")])
    tpath.write_bytes(tpath.read_bytes() + b'{"id":2,"peer_id":')
    _run(tail, FakeTransport({PEER_A: [FakeMsg(1, text="a"), FakeMsg(2, text="b")]}), now=STAMP)
    assert [r["id"] for r in _rows(tpath)] == [1, 2]

    bad = tmp_path / "bad"
    bpath = tg_pull.jsonl_path(bad, PEER_A)
    bpath.parent.mkdir(parents=True)
    bpath.write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(PullRefusal, match="JSONL_CORRUPT"):
        _run(bad, FakeTransport({PEER_A: [FakeMsg(1)]}))


def test_session_out_symlink_and_extension(tmp_path):
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    raw = session_dir / "user"
    got = tg_pull._validate_session(raw)
    assert str(got).endswith(".session")
    link = session_dir / "x.session"
    target = session_dir / "real.session"
    target.write_text("x")
    link.symlink_to(target)
    with pytest.raises(PullRefusal, match="SESSION_SYMLINK"):
        tg_pull._validate_session(link)
    repo_session = tg_pull._repository_root() / "quant-lab" / "leak.session"
    with pytest.raises(PullRefusal, match="SESSION_IN_REPOSITORY"):
        tg_pull._validate_session(repo_session)
    with pytest.raises(PullRefusal, match="OUT_IN_REPOSITORY"):
        tg_pull._validate_out(tg_pull._repository_root() / "quant-lab" / "data")
    outside = tmp_path / "outside-out"
    outside.mkdir()
    linked_out = tmp_path / "link-out"
    linked_out.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PullRefusal, match="OUT_SYMLINK_ESCAPE"):
        tg_pull._validate_out(linked_out)


def test_hidden_prompt_requires_tty(monkeypatch):
    monkeypatch.setattr(tg_pull.sys.stdin, "isatty", lambda: False)
    with pytest.raises(PullRefusal, match="PROMPT_NOT_TTY"):
        tg_pull.hidden_prompt("phone")


def test_getpass_warning_becomes_refusal(monkeypatch):
    monkeypatch.setattr(tg_pull.sys.stdin, "isatty", lambda: True)
    stream = SimpleNamespace(isatty=lambda: True)

    def boom(prompt, stream=None):
        warnings.warn("echo", tg_pull.getpass.GetPassWarning)
        return "secret"

    monkeypatch.setattr(tg_pull.getpass, "getpass", boom)
    with pytest.raises(PullRefusal, match="PROMPT_NOT_TTY"):
        tg_pull.hidden_prompt("phone", stream=stream)


def test_argparse_and_exceptions_do_not_echo_secrets(tmp_path, capsys, monkeypatch):
    sess = tmp_path / "u.session"
    sess.write_text("x")
    out = tmp_path / "o"
    rc = tg_pull.main(
        ["--session", str(sess), "--out", str(out), f"--peer={PEER_A}", "--phone", PHONE, "--api-hash", DIGEST]
    )
    captured = capsys.readouterr()
    assert rc == 2
    assert PHONE not in captured.err and DIGEST not in captured.err
    assert json.loads(captured.err.strip().splitlines()[-1])["reason"] == "ARGS_INVALID"

    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", DIGEST)

    async def boom(**kwargs):
        raise RuntimeError(f"login failed hash={DIGEST} phone={PHONE}")

    monkeypatch.setattr(tg_pull, "_connect_and_pull", boom)
    rc = tg_pull.main(["--session", str(sess), "--out", str(out), f"--peer={PEER_A}"])
    captured = capsys.readouterr()
    assert rc == 2
    assert DIGEST not in captured.err and PHONE not in captured.err
    assert json.loads(captured.err)["reason"] == "PULL_FAILED"


def test_main_flood_hint(tmp_path, capsys, monkeypatch):
    sess = tmp_path / "u.session"
    sess.write_text("x")
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "ab")

    async def flood(**kwargs):
        raise PullAbort("FLOOD_WAIT")

    monkeypatch.setattr(tg_pull, "_connect_and_pull", flood)
    rc = tg_pull.main(["--session", str(sess), "--out", str(tmp_path / "o"), f"--peer={PEER_A}"])
    captured = capsys.readouterr()
    assert rc == 3
    assert FLOOD_WAIT_HINT in captured.err
    assert json.loads([ln for ln in captured.err.splitlines() if ln.startswith("{")][-1])["reason"] in ABORT_CODES


def test_help_restored(capsys):
    with pytest.raises(SystemExit) as exited:
        tg_pull.build_parser().parse_args(["--help"])
    assert exited.value.code == 0
    assert "session" in capsys.readouterr().out


def test_honest_identity_not_official_client():
    ident = honest_identity()
    blob = json.dumps(ident)
    assert ident["app_version"] == APP_VERSION
    assert "Telegram" not in blob and "android" not in ident["device_model"].lower()


def test_hooks_sync_send_future_list_and_keepalive(tmp_path):
    from telethon.tl.functions import PingRequest
    from telethon.tl.functions.help import GetConfigRequest

    sender = FakeSender()
    guard = RpcGuard(None, PullState(tmp_path), sleep=_sleep_log()[0], cap=20)
    tg_pull.hook_sender(sender, guard)
    assert inspect.iscoroutinefunction(sender.send) is False

    async def run():
        ping = PingRequest(1)
        raw = sender.send(ping)
        assert inspect.isawaitable(raw) and not inspect.iscoroutine(raw)
        assert guard.state.rpc_count == 0
        one = GetConfigRequest()
        fut = sender.send(one)
        await fut
        pair = sender.send([one, one])
        assert isinstance(pair, list) and len(pair) == 2
        await pair[0]
        await pair[1]

    asyncio.run(run())
    assert guard.state.rpc_count == 3
    assert sender.calls.count("PingRequest") == 1
    assert sender.calls.count("GetConfigRequest") == 3


def test_usermethods_call_nested_resolve_and_forbidden_write(tmp_path):
    from collections import defaultdict

    from telethon.client.users import UserMethods
    from telethon.tl.functions.help import GetConfigRequest
    from telethon.tl.functions.messages import GetHistoryRequest, SendMessageRequest
    from telethon.tl.types import InputPeerChannel

    class Session:
        async def process_entities(self, result):
            return None

    nested_write = {"hit": False}

    class Dummy:
        def __init__(self, sender):
            self._sender = sender
            self._loop = None
            self.flood_sleep_threshold = 0
            self._flood_waited_requests = {}
            self._no_updates = False
            self._request_retries = 0
            self._raise_last_call_error = True
            self._last_request = 0
            self._log = defaultdict(lambda: logging.getLogger("tg-pull-null"))
            self.session = Session()

            async def _call(sender, request, ordered=False, flood_sleep_threshold=None):
                return await UserMethods._call(self, sender, request, ordered=ordered, flood_sleep_threshold=flood_sleep_threshold)

            self._call = _call

        async def get_input_entity(self, peer):
            if not nested_write["hit"]:
                nested_write["hit"] = True
                await self._call(self._sender, GetConfigRequest())
            return peer

    async def run():
        sender = FakeSender()
        dummy = Dummy(sender)
        guard = RpcGuard(None, PullState(tmp_path), sleep=_sleep_log()[0], cap=20)
        dummy._create_exported_sender = None
        dummy._get_cdn_client = None
        tg_pull.install_rpc_hooks(dummy, guard)
        req = GetHistoryRequest(
            peer=InputPeerChannel(1, 1),
            offset_id=0,
            offset_date=None,
            add_offset=0,
            limit=1,
            max_id=0,
            min_id=0,
            hash=0,
        )
        await dummy._call(dummy._sender, req)
        assert nested_write["hit"] is True
        with pytest.raises(PullRefusal, match="RPC_NOT_ALLOWED"):
            await dummy._call(
                dummy._sender,
                SendMessageRequest(peer=InputPeerChannel(1, 1), message="x", random_id=1),
            )
        assert guard.state.rpc_count >= 2
        assert inspect.iscoroutinefunction(dummy._sender.send) is False

    asyncio.run(run())


def test_get_input_peer_dialogs_on_cache_miss():
    class Client:
        def __init__(self):
            self.dialogs = 0
            self.ready = False
            self.archived = 0

        async def get_input_entity(self, peer):
            if not self.ready:
                raise ValueError("Could not find the input entity for PeerChannel")
            return "in"

        def iter_dialogs(self, limit=None, archived=None, **kwargs):
            async def gen():
                self.dialogs += 1
                if archived:
                    self.archived += 1
                self.ready = True
                yield SimpleNamespace(id=PEER_A)

            return gen()

    client = Client()
    got = asyncio.run(TelethonTransport(client).get_input_peer(PEER_A))
    assert got == "in" and client.dialogs >= 1


def test_interrupt_persists_committed_only(tmp_path):
    out = tmp_path / "out"

    class Boom(FakeTransport):
        def __init__(self):
            super().__init__({PEER_A: [FakeMsg(1), FakeMsg(2)]})
            self.n = 0

        async def invoke(self, request):
            self.n += 1
            if self.n > 1 and type(request).__name__ == "GetHistoryRequest":
                raise RuntimeError("cut")
            return await super().invoke(request)

    boom = Boom()
    with pytest.raises(RuntimeError, match="cut"):
        _run(out, boom, batch=1, wait=1.0)
    assert [r["id"] for r in _rows(tg_pull.jsonl_path(out, PEER_A))] == [1]
    assert load_state(out, [PEER_A], now=STAMP).last_ids[PEER_A] == 1


def test_media_default_photos_and_cap_cli(tmp_path, monkeypatch):
    parser = tg_pull.build_parser()
    ns = parser.parse_args(["--session", str(tmp_path / "s.session"), "--out", str(tmp_path / "o"), f"--peer={PEER_A}"])
    assert ns.media == "photos" and ns.max_requests == 5000 and ns.media_wait == 1.5 and ns.wait == 2.0


def test_unauthorized_peer_refused(tmp_path):
    with pytest.raises(PullRefusal, match="PEER_NOT_AUTHORIZED"):
        _run(tmp_path / "x", FakeTransport({123: []}), peers=[123])


def test_run_lock_nonblocking_and_symlink(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    fd = tg_pull._acquire_run_lock(out)
    try:
        with pytest.raises(PullRefusal, match="PULL_LOCKED"):
            tg_pull._acquire_run_lock(out)
    finally:
        tg_pull._release_run_lock(fd)
    outside = tmp_path / "away.lock"
    outside.write_text("x")
    lock = out / tg_pull.RUN_LOCK_NAME
    if lock.exists():
        lock.unlink()
    lock.symlink_to(outside)
    with pytest.raises(PullRefusal, match="OUT_SYMLINK_ESCAPE"):
        tg_pull._acquire_run_lock(out)


def test_cdn_client_hardened(tmp_path):
    class Inner:
        def __init__(self):
            self.flood_sleep_threshold = 60
            self._request_retries = 5
            self._raise_last_call_error = False
            self._no_updates = False
            self._sender = FakeSender()

            async def _call(*_a, **_k):
                return None

            self._call = _call
            self._create_exported_sender = None
            self._get_cdn_client = None
            self._init_request = SimpleNamespace(**honest_identity())

    class Host:
        def __init__(self):
            self._sender = FakeSender()

            async def _call(*_a, **_k):
                return None

            self._call = _call

            async def original(_cdn):
                return Inner()

            self._get_cdn_client = original
            self._create_exported_sender = None

    async def run():
        host = Host()
        guard = RpcGuard(None, PullState(tmp_path), sleep=_sleep_log()[0], cap=10)
        tg_pull.install_rpc_hooks(host, guard)
        cdn = await host._get_cdn_client(object())
        assert cdn.flood_sleep_threshold == 0
        assert cdn._request_retries == 0
        assert cdn._raise_last_call_error is True
        assert cdn._no_updates is True

    asyncio.run(run())


class FakeTelegramClient:
    last = None

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.connects = 0
        self.disconnected = False
        self._sender = FakeSender()
        self._create_exported_sender = None
        self._get_cdn_client = None
        self._flood_seconds = kwargs.pop("_flood", None) if False else None
        FakeTelegramClient.last = self

        async def _call(sender, request, ordered=False, flood_sleep_threshold=None):
            return SimpleNamespace(messages=[], chats=[])

        self._call = _call

    connect_floods: list[int] = []

    async def connect(self):
        self.connects += 1
        planned = getattr(self, "_connect_floods", None)
        if planned:
            raise FloodWaitError(planned.pop(0))
        if FakeTelegramClient.connect_floods:
            raise FloodWaitError(FakeTelegramClient.connect_floods.pop(0))

    async def is_user_authorized(self):
        return getattr(self, "_authorized", False)

    async def start(self, **kwargs):
        print(f"api_hash={os.environ.get('TG_API_HASH')} phone={PHONE} code={CODE}")
        print(f"stderr-hash={os.environ.get('TG_API_HASH')}", file=__import__("sys").stderr)
        logging.getLogger("telethon.client.auth").error("code=%s phone=%s", CODE, PHONE)
        raise RuntimeError(f"start failed hash={os.environ.get('TG_API_HASH')}")

    async def disconnect(self):
        self.disconnected = True

    async def __call__(self, request, **kwargs):
        return SimpleNamespace(messages=[], chats=[])

    async def get_input_entity(self, peer):
        return peer

    def iter_dialogs(self, **kwargs):
        async def gen():
            if False:
                yield None

        return gen()


def test_connect_short_and_long_flood(tmp_path, monkeypatch):
    import telethon

    sess = tmp_path / "u.session"
    sess.write_text("x")
    out = tmp_path / "o"
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", DIGEST)
    monkeypatch.setattr(telethon, "TelegramClient", FakeTelegramClient)

    FakeTelegramClient.last = None

    async def short(**kwargs):
        client = FakeTelegramClient()
        client._connect_floods = [2]
        client._authorized = True
        sleeps = []

        async def sleep(s):
            sleeps.append(s)

        guard = RpcGuard(None, PullState(out), sleep=sleep, cap=10)
        await guard.retry_flood(client.connect)
        assert sleeps == [pytest.approx(2.2)]
        assert client.connects == 2

    asyncio.run(short())

    client = FakeTelegramClient()
    client._connect_floods = [3601]
    guard = RpcGuard(None, PullState(out), sleep=_sleep_log()[0], cap=10)

    async def long():
        with pytest.raises(PullAbort, match="FLOOD_WAIT"):
            await guard.retry_flood(client.connect)

    asyncio.run(long())
    out.mkdir(parents=True, exist_ok=True)
    state = PullState(out_dir=out, rpc_count=3, last_ids={PEER_A: 9})
    state.persist()
    assert json.loads((out / tg_pull.STATE_NAME).read_text())["rpc_count"] == 3

    FakeTelegramClient.connect_floods = [3601]
    FakeTelegramClient.last = None
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", DIGEST)
    import telethon

    monkeypatch.setattr(telethon, "TelegramClient", FakeTelegramClient)
    sess = tmp_path / "u.session"
    sess.write_text("x")
    out2 = tmp_path / "o2"

    async def go():
        with pytest.raises(PullAbort, match="FLOOD_WAIT"):
            await tg_pull._connect_and_pull(
                session=sess,
                out_dir=out2,
                peers=[PEER_A],
                media="none",
                batch=1,
                wait=1.0,
                media_wait=1.5,
                cap=10,
            )

    asyncio.run(go())
    assert FakeTelegramClient.last is not None
    assert FakeTelegramClient.last.disconnected is True
    assert (out2 / tg_pull.STATE_NAME).is_file()


def test_connect_and_pull_silences_start_and_disconnects(tmp_path, monkeypatch, capsys, caplog):
    import telethon

    FakeTelegramClient.connect_floods = []
    sess = tmp_path / "u.session"
    sess.write_text("x")
    out = tmp_path / "o"
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", DIGEST)
    monkeypatch.setattr(telethon, "TelegramClient", FakeTelegramClient)
    caplog.set_level(logging.DEBUG)
    rc = tg_pull.main(["--session", str(sess), "--out", str(out), f"--peer={PEER_A}"])
    captured = capsys.readouterr()
    blob = captured.out + captured.err + caplog.text
    assert rc == 2
    assert DIGEST not in blob and PHONE not in blob and CODE not in blob
    assert FakeTelegramClient.last is not None
    assert FakeTelegramClient.last.disconnected is True


def test_mutant_serialize_first_seen_cohort_date(tmp_path):
    msg = FakeMsg(1, text="body", from_id=PEER_B, action=Act(), entities=[Ent()], fwd_from=Fwd())

    def invariant():
        got = tg_pull.serialize_message(msg, peer_id=PEER_A, peer_name="n", snapshot_at=STAMP, cohort=tg_pull.cohort_id_for(STAMP))
        assert got["first_seen_at"] is None
        assert got["id"] == 1
        assert got["date"] == UNIX
        assert str(got["cohort"]).startswith("history-pull-")

    orig = tg_pull.serialize_message
    invariant()
    assert_kills(
        invariant,
        orig,
        '"first_seen_at": first_seen_at_for_history(),',
        '"first_seen_at": snapshot_at.isoformat(),',
        lambda b: setattr(tg_pull, "serialize_message", b),
        lambda: setattr(tg_pull, "serialize_message", orig),
    )
    assert_kills(
        invariant,
        orig,
        '"id": int(message.id),',
        '"id": int(message.id) * 0,',
        lambda b: setattr(tg_pull, "serialize_message", b),
        lambda: setattr(tg_pull, "serialize_message", orig),
    )
    orig_cohort = tg_pull.cohort_id_for
    assert_kills(
        invariant,
        orig_cohort,
        'return f"history-pull-{snapshot_at.astimezone(UTC).date().isoformat()}"',
        'return "history-watch-wrong"',
        lambda b: setattr(tg_pull, "cohort_id_for", b),
        lambda: setattr(tg_pull, "cohort_id_for", orig_cohort),
    )


def test_mutant_unix_ms_and_wait_flood_cap(tmp_path):
    orig_unix = tg_pull._unix

    def invariant_unix():
        assert tg_pull._unix(UNIX) == UNIX

    assert_kills(
        invariant_unix,
        orig_unix,
        "return n if n > 0 else None",
        "return n * 1000 if n > 0 else None",
        lambda b: setattr(tg_pull, "_unix", b),
        lambda: setattr(tg_pull, "_unix", orig_unix),
    )

    orig_wait = tg_pull._validate_wait

    def invariant_wait():
        try:
            tg_pull._validate_wait(0.5)
        except PullRefusal:
            return
        raise AssertionError("wait bound missing")

    assert_kills(
        invariant_wait,
        orig_wait,
        "if number is None or number < MIN_WAIT_S:",
        "if number is None or number < 0:",
        lambda b: setattr(tg_pull, "_validate_wait", b),
        lambda: setattr(tg_pull, "_validate_wait", orig_wait),
    )

    orig_batch = tg_pull._validate_batch

    def invariant_batch():
        try:
            tg_pull._validate_batch(101)
        except PullRefusal:
            return
        raise AssertionError("batch bound missing")

    assert_kills(
        invariant_batch,
        orig_batch,
        "if not isinstance(batch, int) or isinstance(batch, bool) or batch < 1 or batch > MAX_BATCH:",
        "if not isinstance(batch, int) or isinstance(batch, bool) or batch < 1:",
        lambda b: setattr(tg_pull, "_validate_batch", b),
        lambda: setattr(tg_pull, "_validate_batch", orig_batch),
    )

    orig_sleep = tg_pull.flood_sleep_seconds

    def invariant_flood_mult():
        assert tg_pull.flood_sleep_seconds(2) == pytest.approx(2.2)

    assert_kills(
        invariant_flood_mult,
        orig_sleep,
        "return seconds * FLOOD_WAIT_MULT",
        "return seconds * 1",
        lambda b: setattr(tg_pull, "flood_sleep_seconds", b),
        lambda: setattr(tg_pull, "flood_sleep_seconds", orig_sleep),
    )

    orig_abort = tg_pull.flood_should_abort

    def invariant_hard():
        assert tg_pull.flood_should_abort(3601) is True

    assert_kills(
        invariant_hard,
        orig_abort,
        "return seconds > FLOOD_WAIT_HARD_S",
        "return False",
        lambda b: setattr(tg_pull, "flood_should_abort", b),
        lambda: setattr(tg_pull, "flood_should_abort", orig_abort),
    )


def test_mutant_cap_and_write_inject_and_allowlist(tmp_path):
    orig_cr = RpcGuard.charge_request
    orig_ch = RpcGuard._charge

    def invariant_cap():
        try:
            _run(tmp_path / "cap-mut", FakeTransport({PEER_A: [FakeMsg(i) for i in range(1, 8)]}), cap=2, batch=1)
        except PullAbort as exc:
            assert exc.reason == "RPC_CAP"
            return
        raise AssertionError("cap missing")

    invariant_cap()
    RpcGuard.charge_request = mutant(orig_cr, "if items and self.state.rpc_count + len(items) > self.cap:", "if False and items and self.state.rpc_count + len(items) > self.cap:")
    RpcGuard._charge = mutant(orig_ch, "if self.state.rpc_count >= self.cap:", "if False and self.state.rpc_count >= self.cap:")
    try:
        with pytest.raises(AssertionError):
            invariant_cap()
    finally:
        RpcGuard.charge_request = orig_cr
        RpcGuard._charge = orig_ch

    orig_peer = tg_pull.pull_peer

    def invariant_write():
        try:
            _run(tmp_path / "w0", FakeTransport({PEER_A: [FakeMsg(1)]}))
        except PullRefusal:
            raise AssertionError("write escaped")

    assert_kills(
        invariant_write,
        orig_peer,
        "input_peer = await transport.get_input_peer(peer_id)",
        "await transport.send_message(peer_id, 'hi')\n    input_peer = await transport.get_input_peer(peer_id)",
        lambda b: setattr(tg_pull, "pull_peer", b),
        lambda: setattr(tg_pull, "pull_peer", orig_peer),
    )

    orig_allowed = tg_pull.rpc_allowed
    send = type("SendMessageRequest", (), {"__module__": "telethon.tl.functions.messages"})()

    def invariant_allow():
        assert tg_pull.rpc_allowed(send) is False

    assert_kills(
        invariant_allow,
        orig_allowed,
        "return all(rpc_qualname(node) in ALLOWED_RPC for node in nodes)",
        "return True",
        lambda b: setattr(tg_pull, "rpc_allowed", b),
        lambda: setattr(tg_pull, "rpc_allowed", orig_allowed),
    )


def test_mutant_session_repo_and_silence(tmp_path, monkeypatch, capsys, caplog):
    orig = tg_pull._validate_session
    repo = tg_pull._repository_root() / "quant-lab" / "leak.session"

    def invariant():
        try:
            tg_pull._validate_session(repo)
        except PullRefusal as exc:
            assert "SESSION_IN_REPOSITORY" in str(exc)
            return
        raise AssertionError("repo session accepted")

    assert_kills(
        invariant,
        orig,
        "if resolved == repo or repo in resolved.parents:",
        "if False and (resolved == repo or repo in resolved.parents):",
        lambda b: setattr(tg_pull, "_validate_session", b),
        lambda: setattr(tg_pull, "_validate_session", orig),
    )

    import telethon

    sess = tmp_path / "u.session"
    sess.write_text("x")
    out = tmp_path / "o2"
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", DIGEST)
    monkeypatch.setattr(telethon, "TelegramClient", FakeTelegramClient)
    caplog.set_level(logging.DEBUG)

    def invariant_silence():
        rc = tg_pull.main(["--session", str(sess), "--out", str(out), f"--peer={PEER_A}"])
        captured = capsys.readouterr()
        blob = captured.out + captured.err + caplog.text
        assert rc == 2
        assert DIGEST not in blob and PHONE not in blob and CODE not in blob

    orig_connect = tg_pull._connect_and_pull
    assert_kills(
        invariant_silence,
        orig_connect,
        "with silence_telethon_io():",
        "with contextlib.nullcontext():",
        lambda b: setattr(tg_pull, "_connect_and_pull", b),
        lambda: setattr(tg_pull, "_connect_and_pull", orig_connect),
    )


def _td_doc(peer_raw: int, text: str, *, edited: int | None = None):
    rec = {
        "id": 2,
        "type": "message",
        "date": "2024-01-01T00:00:00",
        "date_unixtime": str(UNIX),
        "text": text,
        "text_entities": [],
    }
    if edited is not None:
        rec["edited"] = "2024-01-01T01:00:00"
        rec["edited_unixtime"] = str(edited)
    return {
        "name": "Chan",
        "type": "public_channel",
        "id": peer_raw,
        "messages": [rec],
    }


def test_harvest_pull_only_whitelist_and_cross_source(lake):
    from quant_lab.data.sources import canonical_peer_id

    raw_id = 111000001
    peer = canonical_peer_id(raw_id, "public_channel")
    other = canonical_peer_id(111000002, "public_channel")
    base = lake / "import" / "telegram"
    base.mkdir(parents=True)
    (base / "result.json").write_text(json.dumps(_td_doc(raw_id, "td-hello")), encoding="utf-8")
    td_n = harvest.run()["n_messages"]
    assert td_n == 1
    layout = Layout.from_root(lake)
    td_versions = pl.read_parquet(layout.message_version).height

    pull = base / "pull"
    snap = STAMP.isoformat()
    write_jsonl(
        pull / f"{peer}.jsonl",
        [jsonl_row(peer_id=peer, mid=2, text="td-hello", snapshot=snap, cohort="history-pull-2026-09-24")],
    )
    shutil.rmtree(lake / "lake", ignore_errors=True)
    shutil.rmtree(lake / "quarantine", ignore_errors=True)
    mixed = harvest.run()
    assert mixed["n_messages"] == td_n
    assert mixed["source"] == "mixed"
    assert {row["kind"] for row in mixed["sources"]} == {"tdesktop", "pull"}
    assert len(mixed["channel_reports"]) == 1
    assert mixed["channel_reports"][0]["source"] == "mixed"
    assert mixed["channel_reports"][0]["sources"] == ["pull", "tdesktop"]
    mixed_versions = pl.read_parquet(Layout.from_root(lake).message_version)
    assert mixed_versions.height >= td_versions
    assert "td-hello" in mixed_versions["text"].to_list()

    write_jsonl(
        pull / f"{peer}.jsonl",
        [jsonl_row(peer_id=peer, mid=2, text="pull-v2", snapshot=snap, cohort="history-pull-2026-09-24")],
    )
    shutil.rmtree(lake / "lake", ignore_errors=True)
    shutil.rmtree(lake / "quarantine", ignore_errors=True)
    changed = harvest.run()
    assert changed["n_messages"] == td_n + 1
    texts = set(pl.read_parquet(Layout.from_root(lake).message_version)["text"].to_list())
    assert {"td-hello", "pull-v2"} <= texts

    shutil.rmtree(lake / "lake", ignore_errors=True)
    shutil.rmtree(lake / "quarantine", ignore_errors=True)
    (base / "result.json").unlink()
    write_jsonl(
        pull / f"{peer}.jsonl",
        [jsonl_row(peer_id=peer, mid=2, text="only-pull", snapshot=snap, cohort="history-pull-2026-09-24")],
    )
    only = harvest.run()
    assert only["source"] == "pull"
    assert only["n_messages"] == 1
    assert "only-pull" in pl.read_parquet(Layout.from_root(lake).message_version)["text"].to_list()
    assert only["channel_reports"][0]["id"] == peer

    (base / "channels.txt").write_text(f"{raw_id}\n", encoding="utf-8")
    write_jsonl(pull / f"{other}.jsonl", [])
    (pull / f"{other}.jsonl").write_text("{this is not json and must not be parsed}\n", encoding="utf-8")
    shutil.rmtree(lake / "lake", ignore_errors=True)
    shutil.rmtree(lake / "quarantine", ignore_errors=True)
    filtered = harvest.run()
    assert filtered["n_messages"] == 1
    assert other not in filtered["channels"]

    broken = mutant(
        sources.ingest_pull_dir,
        "if allowed_peer_ids is not None and named is not None and named not in allowed_peer_ids:",
        "if False and allowed_peer_ids is not None and named is not None and named not in allowed_peer_ids:",
    )
    orig = harvest.ingest_pull_dir
    harvest.ingest_pull_dir = broken
    try:
        with pytest.raises(harvest.HarvestRefusal):
            harvest.run()
    finally:
        harvest.ingest_pull_dir = orig


def test_harvest_mutants_pull_whitelist_source_dedup(lake):
    from quant_lab.data.sources import canonical_peer_id

    raw_id = 111000001
    peer = canonical_peer_id(raw_id, "public_channel")
    base = lake / "import" / "telegram"
    base.mkdir(parents=True)
    (base / "result.json").write_text(json.dumps(_td_doc(raw_id, "td-hello")), encoding="utf-8")
    pull = base / "pull"
    write_jsonl(
        pull / f"{peer}.jsonl",
        [jsonl_row(peer_id=peer, mid=2, text="td-hello", snapshot=STAMP.isoformat(), cohort="history-pull-2026-09-24")],
    )

    def invariant_mixed():
        shutil.rmtree(lake / "lake", ignore_errors=True)
        shutil.rmtree(lake / "quarantine", ignore_errors=True)
        report = harvest.run()
        assert report["source"] == "mixed"
        assert report["n_messages"] == 1
        assert report["channel_reports"][0]["sources"] == ["pull", "tdesktop"]

    orig_run = harvest.run
    assert_kills(
        invariant_mixed,
        orig_run,
        "pull_msgs, pull_listings = ingest_pull_dir(default_export / \"pull\", allowed_peer_ids=allowed, root=default_export) if pull_files else ([], [])",
        "pull_msgs, pull_listings = ([], [])",
        lambda b: setattr(harvest, "run", b),
        lambda: setattr(harvest, "run", orig_run),
    )
    assert_kills(
        invariant_mixed,
        orig_run,
        "ordinary = _ordinary_for_report(scan.messages, pull_msgs)",
        "ordinary = [m for m in list(scan.messages) + list(pull_msgs) if m.message_type == \"message\"]",
        lambda b: setattr(harvest, "run", b),
        lambda: setattr(harvest, "run", orig_run),
    )
    orig_merged = harvest._merged_channel_reports
    assert_kills(
        invariant_mixed,
        orig_merged,
        'rec["sources"] = kinds',
        'rec["sources"] = []',
        lambda b: setattr(harvest, "_merged_channel_reports", b),
        lambda: setattr(harvest, "_merged_channel_reports", orig_merged),
    )


def test_harvest_pull_symlink_refused(lake, tmp_path):
    base = lake / "import" / "telegram"
    base.mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    pull = base / "pull"
    pull.mkdir()
    (pull / f"{PEER_A}.jsonl").symlink_to(outside)
    with pytest.raises(harvest.HarvestRefusal, match="EXPORT_SYMLINK_ESCAPE"):
        harvest.run()


def test_history_pull_sets_export_snapshot_for_h1(tmp_path):
    path = tmp_path / "p.jsonl"
    snap = datetime(2026, 9, 24, 15, tzinfo=UTC).isoformat()
    write_jsonl(
        path,
        [
            jsonl_row(
                peer_id=PEER_A,
                mid=9,
                text="edited",
                snapshot=snap,
                cohort="history-pull-2026-09-24",
                edit_date=UNIX + 60,
            )
        ],
    )
    raw = next(sources.read_telethon_jsonl(path, root=tmp_path))
    assert raw.first_seen_at is None
    assert raw.export_snapshot_at == datetime(2026, 9, 24, 15, tzinfo=UTC)
    layout = Layout.flat(tmp_path / "lake")
    from quant_lab.data import normalize

    normalize.run(tmp_path, layout, tdesktop_only=True, extra_messages=[raw])
    df = pl.read_parquet(layout.message_version)
    row = df.filter(pl.col("source_id").struct.field("message_id") == 9)
    assert row["time_grade"][0] == "H1"
    assert row["available_at"][0] == datetime(2026, 9, 24, 15, tzinfo=UTC)
