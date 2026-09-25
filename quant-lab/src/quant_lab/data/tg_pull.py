"""限速只读拉取授权频道历史 → Telethon JSONL（供 harvest 从 DATA_ROOT/import/telegram/pull 摄入）。

不读生产 watcher 会话；不发送/编辑/删除/加群。API 凭据只来自环境变量。
登录交互走隐藏输入（phone/code/password 不回显，不进日志）。

RPC 计数边界：MTProtoSender.send 保持同步（返回 Future 或 list[Future]）。
应用 RPC 在 send 入账；Ping/MsgsAck 等控制包不计。FloodWait 只在 _call
（及跨 DC 直接 await send）重试，避免把 send 改成 coroutine。
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import errno
import fcntl
import getpass
import inspect
import io
import json
import logging
import math
import os
import pathlib
import platform
import sys
import tempfile
import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable, Protocol

AUTHORIZED_PEERS = frozenset({-1002136478186, -1002193304023})
DEFAULT_BATCH = 100
MAX_BATCH = 100
DEFAULT_WAIT_S = 2.0
MIN_WAIT_S = 1.0
PHOTO_WAIT_S = 1.5
FLOOD_WAIT_MULT = 1.1
FLOOD_WAIT_HARD_S = 3600
RPC_CAP = 5000
APP_VERSION = "quant-lab-tg-pull/0.1.0"
STATE_NAME = "_rpc_state.json"
RUN_LOCK_NAME = "_pull.lock"
FLOOD_WAIT_HINT = "稍后重跑"

ALLOWED_RPC = frozenset(
    {
        "telethon.tl.functions.InvokeWithLayerRequest",
        "telethon.tl.functions.InitConnectionRequest",
        "telethon.tl.functions.InvokeWithoutUpdatesRequest",
        "telethon.tl.functions.InvokeAfterMsgRequest",
        "telethon.tl.functions.InvokeAfterMsgsRequest",
        "telethon.tl.functions.InvokeWithMessagesRangeRequest",
        "telethon.tl.functions.InvokeWithTakeoutRequest",
        "telethon.tl.functions.help.GetConfigRequest",
        "telethon.tl.functions.help.GetNearestDcRequest",
        "telethon.tl.functions.help.GetCdnConfigRequest",
        "telethon.tl.functions.help.GetAppConfigRequest",
        "telethon.tl.functions.updates.GetStateRequest",
        "telethon.tl.functions.updates.GetDifferenceRequest",
        "telethon.tl.functions.users.GetUsersRequest",
        "telethon.tl.functions.channels.GetChannelsRequest",
        "telethon.tl.functions.messages.GetChatsRequest",
        "telethon.tl.functions.messages.GetHistoryRequest",
        "telethon.tl.functions.messages.GetDialogsRequest",
        "telethon.tl.functions.messages.GetPeerDialogsRequest",
        "telethon.tl.functions.upload.GetFileRequest",
        "telethon.tl.functions.upload.GetCdnFileRequest",
        "telethon.tl.functions.upload.GetCdnFileHashesRequest",
        "telethon.tl.functions.upload.GetFileHashesRequest",
        "telethon.tl.functions.upload.ReuploadCdnFileRequest",
        "telethon.tl.functions.auth.SendCodeRequest",
        "telethon.tl.functions.auth.ResendCodeRequest",
        "telethon.tl.functions.auth.SignInRequest",
        "telethon.tl.functions.auth.CheckPasswordRequest",
        "telethon.tl.functions.auth.ExportAuthorizationRequest",
        "telethon.tl.functions.auth.ImportAuthorizationRequest",
        "telethon.tl.functions.auth.BindTempAuthKeyRequest",
        "telethon.tl.functions.account.GetPasswordRequest",
        "telethon.tl.functions.account.GetPasswordSettingsRequest",
    }
)

TRANSPORT_RPC = frozenset(
    {
        "telethon.tl.functions.PingRequest",
        "telethon.tl.functions.PingDelayDisconnectRequest",
        "telethon.tl.types.MsgsAck",
        "telethon.tl.types.MsgsStateReq",
        "telethon.tl.types.MsgsStateInfo",
        "telethon.tl.types.MsgsAllInfo",
    }
)
TRANSPORT_NAMES = frozenset(
    {
        "PingRequest",
        "PingDelayDisconnectRequest",
        "MsgsAck",
        "MsgsStateReq",
        "MsgsStateInfo",
        "MsgsAllInfo",
        "HttpWaitRequest",
    }
)

JSONL_KEYS = (
    "peer_id",
    "peer_name",
    "id",
    "date",
    "message",
    "first_seen_at",
    "snapshot_at",
    "cohort",
    "edit_date",
    "entities",
    "reply_to_msg_id",
    "grouped_id",
    "fwd_from",
    "media",
    "from_id",
    "action",
)
REQUIRED_JSONL_KEYS = frozenset(JSONL_KEYS)

REFUSAL_CODES = frozenset(
    {
        "ARGS_INVALID",
        "TG_API_MISSING",
        "PEER_REQUIRED",
        "PEER_NOT_AUTHORIZED",
        "SESSION_SYMLINK",
        "SESSION_DIR_MISSING",
        "SESSION_IN_REPOSITORY",
        "OUT_SYMLINK_ESCAPE",
        "OUT_IN_REPOSITORY",
        "BATCH_INVALID",
        "WAIT_TOO_SMALL",
        "MEDIA_WAIT_INVALID",
        "MEDIA_INVALID",
        "CAP_INVALID",
        "RPC_NOT_ALLOWED",
        "JSONL_CORRUPT",
        "PROMPT_NOT_TTY",
        "PULL_LOCKED",
        "PULL_FAILED",
        "CDN_UNHOOKED",
    }
)

ABORT_CODES = frozenset({"FLOOD_WAIT", "RPC_CAP", "PHOTO_FAILED"})


class PullRefusal(ValueError):
    """稳定拒绝码；字符串不得含凭据或消息原文。"""


class PullAbort(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Transport(Protocol):
    async def invoke(self, request: Any) -> Any: ...
    async def get_input_peer(self, peer_id: int) -> Any: ...
    async def download_photo(self, message: Any, dest: pathlib.Path) -> Any: ...


InvokeFn = Callable[[Any], Awaitable[Any]]
SleepFn = Callable[[float], Awaitable[None]]
NowFn = Callable[[], datetime]


def _repository_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[4]


def honest_identity() -> dict[str, str]:
    """真实本机系统信息 + 本工具名；不冒充官方客户端。"""
    uname = platform.uname()
    system = (uname.system or "unknown").strip() or "unknown"
    machine = (uname.machine or "").strip()
    device = f"{system} {machine}".strip()
    return {
        "device_model": device,
        "system_version": (uname.release or "unknown").strip() or "unknown",
        "app_version": APP_VERSION,
        "lang_code": "en",
        "system_lang_code": "en",
    }


def rpc_qualname(request: Any) -> str:
    if isinstance(request, str):
        return request
    cls = request if isinstance(request, type) else type(request)
    return f"{getattr(cls, '__module__', '') or ''}.{cls.__name__}"


def iter_rpc_nodes(request: Any) -> list[Any]:
    nodes: list[Any] = []
    cur = request
    for _ in range(8):
        if cur is None:
            break
        nodes.append(cur)
        inner = getattr(cur, "query", None)
        if inner is None:
            break
        cur = inner
    return nodes


def is_transport_rpc(request: Any) -> bool:
    name = type(request).__name__ if not isinstance(request, str) else request
    if name in TRANSPORT_NAMES:
        return True
    return rpc_qualname(request) in TRANSPORT_RPC


def rpc_allowed(request: Any) -> bool:
    """对象按完全限定类型 + 包装器内层递归判定；裸类名字符串一律拒绝。"""
    if isinstance(request, str):
        return request in ALLOWED_RPC
    nodes = iter_rpc_nodes(request)
    if not nodes:
        return False
    return all(rpc_qualname(node) in ALLOWED_RPC for node in nodes)


def rpc_items(request: Any) -> list[Any]:
    if isinstance(request, (list, tuple)):
        return list(request)
    return [request]


def flood_should_abort(seconds: int) -> bool:
    return seconds > FLOOD_WAIT_HARD_S


def flood_sleep_seconds(seconds: int) -> float:
    return seconds * FLOOD_WAIT_MULT


def first_seen_at_for_history() -> None:
    return None


def cohort_id_for(snapshot_at: datetime) -> str:
    return f"history-pull-{snapshot_at.astimezone(UTC).date().isoformat()}"


def hidden_prompt(label: str, *, stream: Any | None = None) -> str:
    stream = sys.stderr if stream is None else stream
    if not sys.stdin.isatty():
        raise PullRefusal("PROMPT_NOT_TTY")
    if not hasattr(stream, "isatty") or not stream.isatty():
        raise PullRefusal("PROMPT_NOT_TTY")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(f"{label}: ", stream=stream)
        except getpass.GetPassWarning:
            raise PullRefusal("PROMPT_NOT_TTY") from None


def _unix(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = int(value)
        return n if n > 0 else None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
        return int(dt.timestamp())
    return None


def _jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, bytes):
        return None
    if isinstance(obj, datetime):
        return _unix(obj)
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return _jsonable(to_dict())
    return None


def _peer_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        from telethon import utils as tg_utils

        return int(tg_utils.get_peer_id(value))
    except Exception:
        for attr in ("channel_id", "chat_id", "user_id"):
            inner = getattr(value, attr, None)
            if isinstance(inner, int) and not isinstance(inner, bool):
                if attr == "channel_id":
                    return -(1_000_000_000_000 + inner) if inner > 0 else inner
                if attr == "chat_id":
                    return -inner if inner > 0 else inner
                return inner
        return None


def _ensure_contained(root: pathlib.Path, target: pathlib.Path) -> pathlib.Path:
    root_r = root.resolve()
    real = target.resolve()
    if real != root_r and root_r not in real.parents:
        raise PullRefusal("OUT_SYMLINK_ESCAPE")
    repo = _repository_root()
    if real == repo or repo in real.parents:
        raise PullRefusal("OUT_SYMLINK_ESCAPE")
    return real


def _prepare_out_file(out_dir: pathlib.Path, path: pathlib.Path) -> pathlib.Path:
    """写入/截断前拒绝 symlink 逃逸。"""
    if path.is_symlink() or os.path.islink(path):
        raise PullRefusal("OUT_SYMLINK_ESCAPE")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise PullRefusal("OUT_SYMLINK_ESCAPE")
    return _ensure_contained(out_dir, path)


def _atomic_write_text(path: pathlib.Path, text: str, *, out_dir: pathlib.Path | None = None) -> None:
    if out_dir is not None:
        _prepare_out_file(out_dir, path)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _acquire_run_lock(out_dir: pathlib.Path) -> int:
    lock_path = out_dir / RUN_LOCK_NAME
    _prepare_out_file(out_dir, lock_path)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if getattr(exc, "errno", None) in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
            raise PullRefusal("PULL_LOCKED") from None
        raise
    return fd


def _release_run_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _append_jsonl(path: pathlib.Path, record: dict[str, Any], *, out_dir: pathlib.Path) -> None:
    missing = REQUIRED_JSONL_KEYS - set(record)
    if missing:
        raise PullRefusal("JSONL_CORRUPT")
    _prepare_out_file(out_dir, path)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def _load_json(path: pathlib.Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def repair_jsonl(path: pathlib.Path, *, out_dir: pathlib.Path) -> None:
    """只修半行尾；完整但损坏的行拒绝。symlink 在截断前拒绝。"""
    if path.is_symlink() or os.path.islink(path):
        raise PullRefusal("OUT_SYMLINK_ESCAPE")
    if not path.is_file():
        return
    _ensure_contained(out_dir, path)
    data = path.read_bytes()
    if not data:
        return
    if not data.endswith(b"\n"):
        cut = data.rfind(b"\n")
        data = data[: cut + 1] if cut >= 0 else b""
        path.write_bytes(data)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PullRefusal("JSONL_CORRUPT") from exc
        if not isinstance(row, dict) or not REQUIRED_JSONL_KEYS <= set(row):
            raise PullRefusal("JSONL_CORRUPT")


def existing_jsonl_ids(path: pathlib.Path) -> set[int]:
    ids: set[int] = set()
    if not path.is_file():
        return ids
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            ids.add(int(row["id"]))
    return ids


def jsonl_path(out_dir: pathlib.Path, peer_id: int) -> pathlib.Path:
    return out_dir / f"{peer_id}.jsonl"


def ckpt_path(out_dir: pathlib.Path, peer_id: int) -> pathlib.Path:
    return out_dir / f"{peer_id}.ckpt.json"


def state_path(out_dir: pathlib.Path) -> pathlib.Path:
    return out_dir / STATE_NAME


def photo_relpath(peer_id: int, message_id: int) -> str:
    return f"photos/{peer_id}/{message_id}.jpg"


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if isinstance(value, bool) or not math.isfinite(number):
        return None
    return number


def _validate_batch(batch: int) -> int:
    if not isinstance(batch, int) or isinstance(batch, bool) or batch < 1 or batch > MAX_BATCH:
        raise PullRefusal("BATCH_INVALID")
    return batch


def _validate_wait(wait: float) -> float:
    number = _finite(wait)
    if number is None or number < MIN_WAIT_S:
        raise PullRefusal("WAIT_TOO_SMALL")
    return number


def _validate_media_wait(wait: float) -> float:
    number = _finite(wait)
    if number is None or number <= 0:
        raise PullRefusal("MEDIA_WAIT_INVALID")
    return number


def _validate_media(media: str) -> str:
    if media not in {"photos", "none"}:
        raise PullRefusal("MEDIA_INVALID")
    return media


def _validate_cap(cap: int) -> int:
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
        raise PullRefusal("CAP_INVALID")
    return cap


def _validate_peers(values: list[int]) -> list[int]:
    if not values:
        raise PullRefusal("PEER_REQUIRED")
    ordered: list[int] = []
    for peer in values:
        if peer not in AUTHORIZED_PEERS:
            raise PullRefusal("PEER_NOT_AUTHORIZED")
        if peer not in ordered:
            ordered.append(peer)
    return ordered


def _validate_session(path: pathlib.Path) -> pathlib.Path:
    session = pathlib.Path(path).expanduser()
    if not str(session).endswith(".session"):
        session = pathlib.Path(str(session) + ".session")
    if session.is_symlink() or os.path.islink(session):
        raise PullRefusal("SESSION_SYMLINK")
    parent = session.parent
    if not parent.is_dir():
        raise PullRefusal("SESSION_DIR_MISSING")
    resolved = session.resolve()
    repo = _repository_root()
    if resolved == repo or repo in resolved.parents:
        raise PullRefusal("SESSION_IN_REPOSITORY")
    return session


def _validate_out(path: pathlib.Path) -> pathlib.Path:
    out = pathlib.Path(path).expanduser()
    if out.is_symlink() or os.path.islink(out):
        raise PullRefusal("OUT_SYMLINK_ESCAPE")
    resolved = out.resolve()
    repo = _repository_root()
    if resolved == repo or repo in resolved.parents:
        raise PullRefusal("OUT_IN_REPOSITORY")
    return resolved


def _resolve_now(now: datetime | NowFn | None) -> NowFn:
    if now is None:
        return lambda: datetime.now(UTC).replace(microsecond=0)
    if callable(now) and not isinstance(now, datetime):
        def _call() -> datetime:
            stamp = now()
            stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
            return stamp.astimezone(UTC).replace(microsecond=0)

        return _call
    assert isinstance(now, datetime)
    stamp = now if now.tzinfo else now.replace(tzinfo=UTC)
    fixed = stamp.astimezone(UTC).replace(microsecond=0)
    return lambda: fixed


@dataclass
class PullState:
    out_dir: pathlib.Path
    rpc_count: int = 0
    snapshot_at: datetime | None = None
    cohort: str | None = None
    last_ids: dict[int, int] = field(default_factory=dict)
    done: dict[int, bool] = field(default_factory=dict)

    def persist(self) -> None:
        _atomic_write_text(
            state_path(self.out_dir),
            json.dumps({"rpc_count": self.rpc_count}, ensure_ascii=False),
            out_dir=self.out_dir,
        )
        for peer_id, last_id in self.last_ids.items():
            _atomic_write_text(
                ckpt_path(self.out_dir, peer_id),
                json.dumps(
                    {"peer_id": peer_id, "last_id": last_id, "done": bool(self.done.get(peer_id))},
                    ensure_ascii=False,
                ),
                out_dir=self.out_dir,
            )


def load_state(out_dir: pathlib.Path, peers: list[int], *, now: datetime) -> PullState:
    raw = _load_json(state_path(out_dir))
    snapshot = now.astimezone(UTC).replace(microsecond=0)  # 本轮实际拉取时刻，不用旧文件
    cohort = cohort_id_for(snapshot)
    rpc_count = 0  # 每次运行重置
    state = PullState(out_dir=out_dir, rpc_count=rpc_count, snapshot_at=snapshot, cohort=cohort)
    for peer_id in peers:
        out = jsonl_path(out_dir, peer_id)
        repair_jsonl(out, out_dir=out_dir)
        ckpt = _load_json(ckpt_path(out_dir, peer_id))
        try:
            ckpt_last = int(ckpt.get("last_id") or 0)
        except (TypeError, ValueError):
            ckpt_last = 0
        ids = existing_jsonl_ids(out)
        committed = max(ids) if ids else 0
        last_id = committed if ckpt_last > committed else max(ckpt_last, committed)
        state.last_ids[peer_id] = last_id
        state.done[peer_id] = bool(ckpt.get("done"))
    _ = raw
    return state


class RpcGuard:
    def __init__(self, invoke: InvokeFn | None, state: PullState, *, sleep: SleepFn, cap: int = RPC_CAP):
        self._invoke = invoke
        self.state = state
        self._sleep = sleep
        self.cap = cap

    def _charge(self) -> None:
        if self.state.rpc_count >= self.cap:
            raise PullAbort("RPC_CAP")
        self.state.rpc_count += 1

    def validate_one(self, request: Any) -> None:
        if is_transport_rpc(request):
            return
        if not rpc_allowed(request):
            raise PullRefusal("RPC_NOT_ALLOWED")

    def charge_request(self, request: Any) -> None:
        items = [item for item in rpc_items(request) if not is_transport_rpc(item)]
        for item in items:
            self.validate_one(item)
        if items and self.state.rpc_count + len(items) > self.cap:
            raise PullAbort("RPC_CAP")
        for _item in items:
            self._charge()

    async def retry_flood(self, op: Callable[[], Awaitable[Any]]) -> Any:
        while True:
            try:
                return await op()
            except Exception as exc:
                seconds = _flood_seconds(exc)
                if seconds is None:
                    raise
                if flood_should_abort(seconds):
                    raise PullAbort("FLOOD_WAIT") from None
                await self._sleep(flood_sleep_seconds(seconds))

    async def call_through(self, op: Callable[[], Awaitable[Any]], request: Any) -> Any:
        async def attempt() -> Any:
            self.charge_request(request)
            return await op()

        return await self.retry_flood(attempt)

    async def invoke(self, request: Any) -> Any:
        if self._invoke is None:
            raise PullRefusal("RPC_NOT_ALLOWED")
        return await self.call_through(lambda: self._invoke(request), request)


def _flood_seconds(exc: BaseException) -> int | None:
    name = type(exc).__name__
    if name in {"FloodWaitError", "FloodPremiumWaitError"}:
        try:
            return int(getattr(exc, "seconds", 0) or 0)
        except (TypeError, ValueError):
            return 0
    try:
        from telethon.errors import FloodPremiumWaitError, FloodWaitError
    except ImportError:
        return None
    if isinstance(exc, (FloodWaitError, FloodPremiumWaitError)):
        try:
            return int(getattr(exc, "seconds", 0) or 0)
        except (TypeError, ValueError):
            return 0
    return None


def _has_photo(message: Any) -> bool:
    if getattr(message, "photo", None) is not None:
        return True
    media = getattr(message, "media", None)
    if media is None:
        return False
    name = type(media).__name__
    if name in {"MessageMediaPhoto", "Photo"}:
        return True
    return getattr(media, "photo", None) is not None


def _reply_to_msg_id(message: Any) -> int | None:
    value = getattr(message, "reply_to_msg_id", None)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    header = getattr(message, "reply_to", None)
    inner = getattr(header, "reply_to_msg_id", None)
    if isinstance(inner, int) and not isinstance(inner, bool):
        return inner
    return None


def _action_name(message: Any) -> str | None:
    action = getattr(message, "action", None)
    if action is None:
        return None
    if isinstance(action, str):
        return action
    return type(action).__name__


def _fwd_from_record(message: Any) -> dict[str, Any] | None:
    fwd = getattr(message, "fwd_from", None) or getattr(message, "forward", None)
    if fwd is None:
        return None
    original = getattr(fwd, "original_fwd", None) or fwd
    rec: dict[str, Any] = {}
    name = getattr(original, "from_name", None)
    if name:
        rec["from_name"] = str(name)
    from_id = _peer_int(getattr(original, "from_id", None))
    if from_id is not None:
        rec["from_id"] = from_id
    post = getattr(original, "channel_post", None)
    if isinstance(post, int) and not isinstance(post, bool):
        rec["channel_post"] = post
    date = _unix(getattr(original, "date", None))
    if date is not None:
        rec["date"] = date
    return rec or None


def serialize_message(
    message: Any,
    *,
    peer_id: int,
    peer_name: str,
    snapshot_at: datetime,
    cohort: str,
    media: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    grouped = getattr(message, "grouped_id", None)
    if not isinstance(grouped, int) or isinstance(grouped, bool):
        grouped = None
    rec: dict[str, Any] = {
        "peer_id": peer_id,
        "peer_name": peer_name,
        "id": int(message.id),
        "date": _unix(getattr(message, "date", None)),
        "message": getattr(message, "message", None) or "",
        "first_seen_at": first_seen_at_for_history(),
        "snapshot_at": snapshot_at.astimezone(UTC).isoformat(),
        "cohort": cohort,
        "edit_date": _unix(getattr(message, "edit_date", None)),
        "entities": _jsonable(getattr(message, "entities", None) or []) or [],
        "reply_to_msg_id": _reply_to_msg_id(message),
        "grouped_id": grouped,
        "fwd_from": _fwd_from_record(message),
        "media": list(media or []),
        "from_id": _peer_int(getattr(message, "from_id", None)),
        "action": _action_name(message),
    }
    return rec


def _history_request(peer: Any, *, offset_id: int, limit: int) -> Any:
    from telethon.tl.functions.messages import GetHistoryRequest

    return GetHistoryRequest(
        peer=peer,
        offset_id=offset_id,
        offset_date=None,
        add_offset=-limit,
        limit=limit,
        max_id=0,
        min_id=0,
        hash=0,
    )


def _is_empty_message(message: Any) -> bool:
    name = type(message).__name__
    return name == "MessageEmpty" or getattr(message, "id", None) in (None, 0)


def _attach_flood_retry(result: Any, fire: Callable[[], Any], guard: RpcGuard) -> Any:
    """send 仍返回 Future/list[Future]；单 Future 在 await 时重试 FloodWait 并再次计费。"""
    if isinstance(result, list):
        return result
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return result
    out = loop.create_future()

    def chain(source: Any) -> None:
        def _cb(fut: Any) -> None:
            if out.cancelled():
                return
            if fut.cancelled():
                out.cancel()
                return
            exc = fut.exception()
            if exc is None:
                out.set_result(fut.result())
                return
            seconds = _flood_seconds(exc)
            if seconds is None:
                out.set_exception(exc)
                return
            if flood_should_abort(seconds):
                out.set_exception(PullAbort("FLOOD_WAIT"))
                return

            async def retry() -> None:
                try:
                    await guard._sleep(flood_sleep_seconds(seconds))
                    nxt = fire()
                    if isinstance(nxt, list):
                        out.set_exception(PullAbort("FLOOD_WAIT"))
                        return
                    chain(nxt)
                except Exception as err:
                    if not out.done():
                        out.set_exception(err)

            loop.create_task(retry())

        source.add_done_callback(_cb)

    chain(result)
    return out


def hook_sender(sender: Any, guard: RpcGuard) -> None:
    """保持 send 同步：返回 Future 或 list[Future]，供 keepalive 无 await 调用。"""
    if sender is None or getattr(sender, "_ql_pull_hooked", False):
        return
    original = sender.send

    def send(request: Any, ordered: bool = False):
        items = [item for item in rpc_items(request) if not is_transport_rpc(item)]
        for item in items:
            guard.validate_one(item)
        if items and guard.state.rpc_count + len(items) > guard.cap:
            raise PullAbort("RPC_CAP")

        def fire():
            guard.charge_request(request)
            return original(request, ordered=ordered)

        result = fire()
        if not items:
            return result
        return _attach_flood_retry(result, fire, guard)

    sender.send = send
    sender._ql_pull_hooked = True


def install_rpc_hooks(client: Any, guard: RpcGuard) -> None:
    original_call = client._call

    async def guarded_call(sender, request, ordered=False, flood_sleep_threshold=None):
        hook_sender(sender, guard)
        return await guard.retry_flood(
            lambda: original_call(sender, request, ordered=ordered, flood_sleep_threshold=0)
        )

    client._call = guarded_call
    hook_sender(getattr(client, "_sender", None), guard)
    _install_exported_sender(client, guard)
    _install_cdn_client(client, guard)


async def _disconnect_quiet(obj: Any) -> None:
    disconnect = getattr(obj, "disconnect", None)
    if not callable(disconnect):
        return
    result = disconnect()
    if inspect.isawaitable(result):
        await result


def _harden_telegram_client(client: Any) -> None:
    client.flood_sleep_threshold = 0
    client._request_retries = 0
    client._raise_last_call_error = True
    client._no_updates = True
    ident = honest_identity()
    init = getattr(client, "_init_request", None)
    if init is not None:
        for key, value in ident.items():
            if hasattr(init, key):
                setattr(init, key, value)


def _install_exported_sender(client: Any, guard: RpcGuard) -> None:
    async def create_exported(dc_id):
        from telethon.network.mtprotosender import MTProtoSender
        from telethon.tl import functions
        from telethon.tl.alltlobjects import LAYER

        sender = None
        try:
            dc = await client._get_dc(dc_id)
            sender = MTProtoSender(None, loggers=client._log)
            await sender.connect(
                client._connection(
                    dc.ip_address,
                    dc.port,
                    dc.id,
                    loggers=client._log,
                    proxy=client._proxy,
                    local_addr=client._local_addr,
                )
            )
            hook_sender(sender, guard)
            auth = await client(functions.auth.ExportAuthorizationRequest(dc_id))
            client._init_request.query = functions.auth.ImportAuthorizationRequest(id=auth.id, bytes=auth.bytes)
            req = functions.InvokeWithLayerRequest(LAYER, client._init_request)
            fut = sender.send(req)
            await fut
            return sender
        except BaseException:
            if sender is not None:
                await _disconnect_quiet(sender)
            raise

    client._create_exported_sender = create_exported


def _install_cdn_client(client: Any, guard: RpcGuard) -> None:
    original = getattr(client, "_get_cdn_client", None)
    if original is None or not callable(original):
        return

    async def get_cdn(cdn_redirect):
        cdn_client = await original(cdn_redirect)
        if cdn_client is None or getattr(cdn_client, "_call", None) is None:
            raise PullRefusal("CDN_UNHOOKED")
        _harden_telegram_client(cdn_client)
        install_rpc_hooks(cdn_client, guard)
        return cdn_client

    client._get_cdn_client = get_cdn


@contextlib.contextmanager
def silence_telethon_io():
    """关掉 Telethon 日志与 stdio；getpass 必须在进入前提取 tty stream。"""
    prev_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    saved: dict[str, tuple[bool, int, list[Any], bool]] = {}
    for name, obj in list(logging.root.manager.loggerDict.items()):
        if not isinstance(name, str) or not (name == "telethon" or name.startswith("telethon.")):
            continue
        if not isinstance(obj, logging.Logger):
            continue
        saved[name] = (obj.disabled, obj.level, list(obj.handlers), obj.propagate)
        obj.disabled = True
        obj.handlers.clear()
        obj.propagate = False
        obj.setLevel(logging.CRITICAL + 1)
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()
    try:
        yield
    finally:
        sys.stdout, sys.stderr = old_out, old_err
        logging.disable(prev_disable)
        for name, (disabled, level, handlers, propagate) in saved.items():
            log = logging.getLogger(name)
            log.disabled = disabled
            log.setLevel(level)
            log.handlers[:] = handlers
            log.propagate = propagate


def attach_invoke_guard(transport: Transport, guard: RpcGuard) -> None:
    original = transport.invoke
    guard._invoke = original

    async def wrapped(request: Any) -> Any:
        return await guard.invoke(request)

    transport.invoke = wrapped  # type: ignore[method-assign]


async def pull_peer(
    *,
    peer_id: int,
    transport: Transport,
    state: PullState,
    media: str,
    batch: int,
    wait: float,
    media_wait: float,
    sleep: SleepFn,
    seen_ids: set[int],
    now: NowFn,
) -> None:
    input_peer = await transport.get_input_peer(peer_id)
    peer_name = str(peer_id)
    out = jsonl_path(state.out_dir, peer_id)
    last_id = state.last_ids.get(peer_id, 0)
    while True:
        request = _history_request(input_peer, offset_id=(last_id + 1 if last_id else 1), limit=batch)
        result = await transport.invoke(request)
        raw_msgs = [m for m in (getattr(result, "messages", None) or []) if not _is_empty_message(m)]
        chats = list(getattr(result, "chats", None) or [])
        for chat in chats:
            cid = _peer_int(chat)
            title = getattr(chat, "title", None)
            if cid == peer_id and title:
                peer_name = str(title)
        page = [m for m in raw_msgs if int(m.id) > last_id and int(m.id) not in seen_ids]
        page.sort(key=lambda m: int(m.id))
        if not page:
            state.done[peer_id] = True
            state.persist()
            return
        for message in page:
            mid = int(message.id)
            if mid in seen_ids:
                continue
            media_refs: list[dict[str, Any]] = []
            if media == "photos" and _has_photo(message):
                rel = photo_relpath(peer_id, mid)
                dest = _prepare_out_file(state.out_dir, state.out_dir / rel)
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    actual = await transport.download_photo(message, dest)
                except PullAbort:
                    raise
                except PullRefusal:
                    raise
                except Exception:
                    raise PullAbort("PHOTO_FAILED") from None
                if actual is None:
                    raise PullAbort("PHOTO_FAILED")
                actual_path = pathlib.Path(actual)
                if not actual_path.is_file():
                    raise PullAbort("PHOTO_FAILED")
                contained = _ensure_contained(state.out_dir, actual_path)
                try:
                    size = contained.stat().st_size
                except OSError:
                    raise PullAbort("PHOTO_FAILED") from None
                media_refs.append(
                    {"kind": "photo", "path": str(contained.relative_to(state.out_dir.resolve())), "size": size}
                )
                await sleep(media_wait)
            stamp = now()  # 每条实际拉取时刻，不用旧 snapshot
            record = serialize_message(
                message,
                peer_id=peer_id,
                peer_name=peer_name,
                snapshot_at=stamp,
                cohort=cohort_id_for(stamp),
                media=media_refs,
            )
            _append_jsonl(out, record, out_dir=state.out_dir)
            seen_ids.add(mid)
            last_id = mid
            state.last_ids[peer_id] = last_id
            state.persist()
        await sleep(wait)


async def pull_history(
    *,
    peers: list[int],
    out_dir: pathlib.Path,
    media: str = "photos",
    batch: int = DEFAULT_BATCH,
    wait: float = DEFAULT_WAIT_S,
    media_wait: float = PHOTO_WAIT_S,
    transport: Transport,
    sleep: SleepFn | None = None,
    now: datetime | NowFn | None = None,
    cap: int | None = None,
    state: PullState | None = None,
    guard: RpcGuard | None = None,
    attach: bool = True,
    hold_lock: bool = True,
) -> PullState:
    peers = _validate_peers(list(peers))
    media = _validate_media(media)
    batch = _validate_batch(batch)
    wait = _validate_wait(wait)
    media_wait = _validate_media_wait(media_wait)
    cap_n = _validate_cap(RPC_CAP if cap is None else cap)
    out_dir = _validate_out(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    lock_fd = _acquire_run_lock(out_dir) if hold_lock else None
    sleeper = sleep or asyncio.sleep
    clock = _resolve_now(now)
    stamp = clock()
    try:
        if state is None:
            state = load_state(out_dir, peers, now=stamp)
        if guard is None:
            guard = RpcGuard(transport.invoke, state, sleep=sleeper, cap=cap_n)
            if attach:
                attach_invoke_guard(transport, guard)
        try:
            for peer_id in peers:
                seen = existing_jsonl_ids(jsonl_path(out_dir, peer_id))  # 不因上次 done 跳过新消息
                await pull_peer(
                    peer_id=peer_id,
                    transport=transport,
                    state=state,
                    media=media,
                    batch=batch,
                    wait=wait,
                    media_wait=media_wait,
                    sleep=sleeper,
                    seen_ids=seen,
                    now=clock,
                )
        except PullAbort:
            raise
        finally:
            state.persist()
        return state
    finally:
        if lock_fd is not None:
            _release_run_lock(lock_fd)


class TelethonTransport:
    def __init__(self, client: Any, peers: list[int] | None = None):
        self.client = client
        self._needed = set(peers or AUTHORIZED_PEERS)

    async def invoke(self, request: Any) -> Any:
        return await self.client(request)

    async def get_input_peer(self, peer_id: int) -> Any:
        from telethon.tl.types import PeerChannel
        from telethon.utils import resolve_id

        bare, _kind = resolve_id(peer_id)
        peer = PeerChannel(bare)
        try:
            return await self.client.get_input_entity(peer)
        except (ValueError, TypeError):
            await self._fill_dialog_cache(self._needed | {peer_id})
            return await self.client.get_input_entity(peer)

    async def _fill_dialog_cache(self, needed: set[int]) -> None:
        found: set[int] = set()

        async def consume(iterator: Any) -> None:
            async for dialog in iterator:
                pid = getattr(dialog, "id", None)
                if isinstance(pid, int) and pid in needed:
                    found.add(pid)
                if needed <= found:
                    return

        await consume(self.client.iter_dialogs(limit=None))
        if not needed <= found:
            await consume(self.client.iter_dialogs(limit=None, archived=True))

    async def download_photo(self, message: Any, dest: pathlib.Path) -> pathlib.Path:
        result = await self.client.download_media(message, file=str(dest))
        if not result:
            raise PullAbort("PHOTO_FAILED")
        return pathlib.Path(result)


def _env_api() -> tuple[int, str]:
    raw_id = os.environ.get("TG_API_ID")
    raw_hash = os.environ.get("TG_API_HASH")
    if not raw_id or not str(raw_id).strip() or not raw_hash or not str(raw_hash).strip():
        raise PullRefusal("TG_API_MISSING")
    try:
        api_id = int(str(raw_id).strip())
    except (TypeError, ValueError) as exc:
        raise PullRefusal("TG_API_MISSING") from exc
    if api_id <= 0:
        raise PullRefusal("TG_API_MISSING")
    return api_id, str(raw_hash).strip()


class _QuietParser(argparse.ArgumentParser):
    """错误信息不回显未知 flag 的原始值（可能含 phone/hash/code）。"""

    def error(self, message: str) -> None:
        raise PullRefusal("ARGS_INVALID")


def build_parser() -> argparse.ArgumentParser:
    parser = _QuietParser(prog="quant_lab.data.tg_pull", description=__doc__, add_help=True)
    parser.add_argument("--session", required=True, help="仓库外 session 路径")
    parser.add_argument("--out", required=True)
    parser.add_argument("--peer", action="append", required=True, type=int)
    parser.add_argument("--media", choices=("photos", "none"), default="photos")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--wait", type=float, default=DEFAULT_WAIT_S)
    parser.add_argument("--media-wait", type=float, default=PHOTO_WAIT_S, dest="media_wait")
    parser.add_argument("--max-requests", type=int, default=RPC_CAP, dest="max_requests")
    return parser


async def _connect_and_pull(
    *,
    session: pathlib.Path,
    out_dir: pathlib.Path,
    peers: list[int],
    media: str,
    batch: int,
    wait: float,
    media_wait: float,
    cap: int,
) -> PullState:
    from telethon import TelegramClient

    lock_fd = None
    client: Any = None
    state: PullState | None = None
    sleeper = asyncio.sleep
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        lock_fd = _acquire_run_lock(out_dir)
        stamp = datetime.now(UTC).replace(microsecond=0)
        state = load_state(out_dir, peers, now=stamp)
        guard = RpcGuard(None, state, sleep=sleeper, cap=cap)
        api_id, api_hash = _env_api()
        identity = honest_identity()
        client = TelegramClient(
            str(session),
            api_id,
            api_hash,
            receive_updates=False,
            flood_sleep_threshold=0,
            request_retries=0,
            raise_last_call_error=True,
            auto_reconnect=False,
            **identity,
        )
        install_rpc_hooks(client, guard)
        prompt_stream = sys.stderr
        with silence_telethon_io():
            await guard.retry_flood(client.connect)
            hook_sender(getattr(client, "_sender", None), guard)
            if not await client.is_user_authorized():
                await client.start(
                    phone=lambda: hidden_prompt("phone", stream=prompt_stream),
                    password=lambda: hidden_prompt("password", stream=prompt_stream),
                    code_callback=lambda: hidden_prompt("code", stream=prompt_stream),
                )
            transport = TelethonTransport(client, peers=peers)
            return await pull_history(
                peers=peers,
                out_dir=out_dir,
                media=media,
                batch=batch,
                wait=wait,
                media_wait=media_wait,
                transport=transport,
                sleep=sleeper,
                cap=cap,
                state=state,
                guard=guard,
                attach=False,
                hold_lock=False,
            )
    finally:
        try:
            if state is not None:
                state.persist()
        finally:
            try:
                if client is not None:
                    await _disconnect_quiet(client)
            finally:
                if lock_fd is not None:
                    _release_run_lock(lock_fd)


def _refusal_code(exc: BaseException) -> str:
    code = exc.args[0] if getattr(exc, "args", None) else "PULL_FAILED"
    if not isinstance(code, str) or code not in REFUSAL_CODES:
        return "PULL_FAILED"
    return code


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        session = _validate_session(pathlib.Path(args.session))
        peers = _validate_peers(list(args.peer))
        media = _validate_media(args.media)
        batch = _validate_batch(args.batch)
        wait = _validate_wait(args.wait)
        media_wait = _validate_media_wait(args.media_wait)
        cap = _validate_cap(args.max_requests)
        _env_api()
        out_dir = _validate_out(pathlib.Path(args.out))
        asyncio.run(
            _connect_and_pull(
                session=session,
                out_dir=out_dir,
                peers=peers,
                media=media,
                batch=batch,
                wait=wait,
                media_wait=media_wait,
                cap=cap,
            )
        )
    except PullRefusal as exc:
        print(json.dumps({"status": "refused", "reason": _refusal_code(exc)}), file=sys.stderr)
        return 2
    except PullAbort as exc:
        reason = exc.reason if exc.reason in ABORT_CODES else "PULL_FAILED"
        if reason == "FLOOD_WAIT":
            print(FLOOD_WAIT_HINT, file=sys.stderr)
        print(json.dumps({"status": "aborted", "reason": reason}), file=sys.stderr)
        return 3 if reason in ABORT_CODES else 2
    except Exception:
        print(json.dumps({"status": "refused", "reason": "PULL_FAILED"}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
