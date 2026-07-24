#!/usr/bin/env python3
"""Feed NEW telegram signals from the watcher's SQLite to the Hermes agent.

Hermes (trader profile) is the trading decision maker: for every new channel
message it must (1) tell the user on Telegram what arrived, (2) decide whether
it is an actionable signal, (3) place the order through the v3-trader skill if
so, and (4) report the outcome. This feeder only transports messages — it makes
no trading judgement itself.

Read-only on the watcher DB. Cursor = "received_at|signal_id" of the last
handled row. A message that keeps failing is skipped after MAX_ATTEMPTS so one
poison message cannot wedge the queue (skips are logged loudly).
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

WATCHER_DB = "/var/lib/docker/volumes/trader_signal-data/_data/signal_store.db"
WATCHER_ROOT = "/var/lib/docker/volumes/trader_signal-data/_data"  # container /data -> here
V3_MEDIA = "/srv/trader-v3/media"
STATE = "/srv/trader-v3/scripts/.hermes_feeder_cursor"
LOCK = "/srv/trader-v3/scripts/.hermes_feeder.lock"
CHANNEL_CONTEXT_DIR = "/srv/trader-v3/scripts/.channel_ctx"
HERMES_OUTPUT_DIR = "/srv/hermes/profiles/trader/cron/output"
HERMES_BIN = os.environ.get("HERMES_BIN", "/srv/hermes/hermes-agent/venv/bin/hermes")
# HERMES_HOME must match the gateway service's env (unit sets it to the profile
# dir) — the cron job store lives under it; a job created under a different
# home is invisible to the gateway scheduler and never runs.
HERMES_ENV = {
    **os.environ,
    "HERMES_PROFILE": "trader",
    "HERMES_ACCEPT_HOOKS": "1",
    "HERMES_HOME": os.environ.get("HERMES_HOME", "/srv/hermes/profiles/trader"),
}
POLL_SECONDS = 5
RUN_TIMEOUT = 240
MAX_ATTEMPTS = 3
HOLD_SECONDS = 25
BATCH_MAX_MESSAGES = 6
BATCH_MAX_SPAN_SECONDS = 120
CONTEXT_TAIL_CHARS = 1500
CONTEXT_COMPRESS_CHARS = 6000
CONTEXT_IDLE_SECONDS = 30 * 60
CONTEXT_KEEP_ENTRIES = 10
RESPONSE_CAPTURE_SECONDS = 420
# LLM-backend failure text markers: such a "response" means the agent never
# actually reasoned about the message — treat as delivery failure and retry
# (with pacing) instead of advancing the cursor past a dropped signal.
BRAIN_FAILURE_MARKERS = ("API call failed", "auth_unavailable", "no auth available")
BRAIN_RETRY_DELAY_SECONDS = 120

# Hermes' cron guard hard-blocks prompts containing these invisible unicode chars
# (hermes-agent tools/cronjob_tools.py _CRON_INVISIBLE_CHARS). Ordinary channel
# messages hit this via emoji ZWJ sequences (for example, heart-on-fire carries U+200D), which
# silently dropped a real trading signal (Titan m4374, 2026-07-04). Strip exactly
# that set — emoji may degrade visually, the text content is untouched.
_INVISIBLE_TABLE = {
    c: None
    for c in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF,
              0x202A, 0x202B, 0x202C, 0x202D, 0x202E)
}


def sanitize_prompt(prompt: str) -> str:
    return prompt.translate(_INVISIBLE_TABLE)


BLOCKED_NOTICE_TEMPLATE = """一条频道消息被安全防护拦截,没有进入正常处理流程。请立刻用口语化短消息(1~2行)告知用户:
频道: {channel_name}
消息ID: {message_id}
拦截原因: {reason}
告诉用户这条消息系统没有自动处理,如果重要请他自己查看频道原文并口头下指令。不要编造消息内容。"""

PROMPT_TEMPLATE = """收到新的交易频道消息,你是交易决策者,请处理:

频道: {channel_name} ({channel_id})
批次消息ID: {message_ids}
时间范围: {time_range}

频道近期上下文(仅供参考,不是新指令):
{channel_context}

正文逐条列出:
{message_block}
{media_block}
处理要求:
1. 先判断这是什么: 可执行的交易信号 / 已有仓位的更新指令(止盈止损调整、平仓) / 行情分析 / 噪音。
2. 如果是可执行信号或仓位指令: 使用 v3-trader skill 的 v3_trade.py 执行。开仓不传 --notional(系统按风险配置自动定量),但信号给了止损就必须传 --sl;信号无止损时才显式给一个小额 --notional 并在回复里说明。必带 --reason 引用本批次消息;--ref 必须用【对应那条消息自己】的 tg-<signal_id>(正文块中逐条给出),同一批次里不同消息的交易禁止共用 ref(平仓/减仓同样)。信号给了多个入场价(如首次入场+加仓价)时每个价位都要单独挂一笔,--ref 加档位后缀(tg-{signal_id}-e1、-e2)避免被幂等去重。缺少关键参数(如方向或币种)时不要猜,标记为无法执行。
3. 纯图片消息若上下文显示同频道刚有文字消息则视为其附图,结合判断,不当独立信号。
4. “锁定X%利润”= 按当前仓位的X%执行部分平仓,不算缺参数。
5. 除非信号明确给出新的止盈价位,不要替换原止盈计划,尤其不要拿“未来反向入场区”当止盈。
6. 最终回复会自动发到用户 Telegram,必须是口语化短消息(2~4行): 一句消息摘要 + 你的判断 + 结果(成交价/数量,或不操作的原因)。禁止粘贴 JSON、字段名、UUID 全串、脚本原始输出;需要单号时只给 intent 前8位。噪音就一句话,例如「🔕 XX频道: 行情闲聊,不操作」。"""


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def load_cursor() -> str | None:
    try:
        return open(STATE).read().strip() or None
    except FileNotFoundError:
        return None


def save_cursor(value: str) -> None:
    tmp = STATE + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(value)
    os.replace(tmp, STATE)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{WATCHER_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def latest_cursor(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT received_at, signal_id FROM signals ORDER BY received_at DESC, signal_id DESC LIMIT 1"
    ).fetchone()
    return f"{row['received_at']}|{row['signal_id']}" if row else None


def fetch_new(conn: sqlite3.Connection, cursor: str | None, limit: int = 50) -> list[sqlite3.Row]:
    if cursor:
        ts, _, sid = cursor.partition("|")
        return conn.execute(
            "SELECT * FROM signals WHERE (received_at, signal_id) > (?, ?) "
            "ORDER BY received_at, signal_id LIMIT ?",
            (ts, sid, limit),
        ).fetchall()
    return []


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _payload(sig: Any) -> dict[str, Any]:
    raw = _row_get(sig, "payload")
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _parse_received_at(value: Any) -> datetime:
    """Never raises: a single malformed watcher row must not crash-loop the
    whole feeder (adversarial-review P1-1). Unparseable -> epoch 0 (very old),
    i.e. the row is delivered immediately instead of wedging the queue."""
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return datetime.fromtimestamp(0, tz=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _coerce_now(now: Any = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(float(now), tz=timezone.utc)


def _signal_cursor(sig: Any) -> str:
    return f"{_row_get(sig, 'received_at')}|{_row_get(sig, 'signal_id')}"


def _signal_channel(sig: Any) -> str:
    payload = _payload(sig)
    return str(payload.get("source_channel_id") or payload.get("source_channel_name") or "unknown")


def _signal_message_id(sig: Any) -> str:
    payload = _payload(sig)
    return str(payload.get("source_message_id") or _row_get(sig, "signal_id"))


def select_deliverable_batch(rows: list[Any], now: datetime | None = None) -> list[Any]:
    if not rows:
        return []
    current = _coerce_now(now)
    first = rows[0]
    first_channel = _signal_channel(first)
    batch = [first]
    first_at = _parse_received_at(_row_get(first, "received_at"))
    previous_at = first_at
    capped = False
    for sig in rows[1:]:
        if _signal_channel(sig) != first_channel:
            break
        received_at = _parse_received_at(_row_get(sig, "received_at"))
        if abs((received_at - previous_at).total_seconds()) > HOLD_SECONDS:
            break
        # Hard caps: a channel that posts continuously must not chain the batch
        # (and therefore its own delivery) forward indefinitely.
        if (
            len(batch) >= BATCH_MAX_MESSAGES
            or (received_at - first_at).total_seconds() > BATCH_MAX_SPAN_SECONDS
        ):
            capped = True
            break
        batch.append(sig)
        previous_at = received_at
    newest_at = _parse_received_at(_row_get(batch[-1], "received_at"))
    age = (current - newest_at).total_seconds()
    # A row stamped in the future (bad clock upstream) would otherwise hold the
    # queue until the wall clock catches up: deliver it once it is >5min ahead.
    if not capped and age < HOLD_SECONDS and age > -300:
        return []
    return batch


def _copy_media(payload: dict[str, Any]) -> list[str]:
    paths = []
    os.makedirs(V3_MEDIA, exist_ok=True)
    for m in payload.get("media") or []:
        path = m.get("path") or ""
        src = WATCHER_ROOT + path[5:] if path.startswith("/data/") else path
        if not src or not os.path.isfile(src):
            continue
        dst = os.path.join(V3_MEDIA, os.path.basename(src))
        try:
            if not os.path.exists(dst):
                shutil.copy2(src, dst)
            paths.append(dst)
        except OSError as exc:
            log(f"media copy failed {src}: {exc}")
    return paths


def channel_context_path(channel_id: Any, channel_name: Any = None) -> str:
    key = str(channel_id or channel_name or "unknown")
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    slug_source = str(channel_name or channel_id or "unknown").lower()
    slug = re.sub(r"[^a-z0-9._-]+", "-", slug_source).strip("-.") or "channel"
    return os.path.join(CHANNEL_CONTEXT_DIR, f"{slug}-{digest}.md")


def _context_tail(channel_id: Any, channel_name: Any) -> str:
    path = channel_context_path(channel_id, channel_name)
    try:
        data = open(path).read()
    except FileNotFoundError:
        return "(无)"
    return data[-CONTEXT_TAIL_CHARS:] if data else "(无)"


def _message_summary(batch: list[Any], limit: int = 200) -> str:
    parts = []
    for sig in batch:
        payload = _payload(sig)
        text = (payload.get("raw_text") or "").strip()
        if not text:
            text = "(无文字,仅图片)" if payload.get("media") else "(无文字)"
        text = " ".join(text.split())  # newline flattening: a crafted message must not
        # be able to forge extra context entries / fake "回复:" records (review P2-2)
        parts.append(f"{_signal_message_id(sig)}:{text}")
    summary = " / ".join(parts)
    return summary[:limit]


def build_prompt(sig_or_batch: Any) -> str:
    batch = sig_or_batch if isinstance(sig_or_batch, list) else [sig_or_batch]
    payloads = [_payload(sig) for sig in batch]
    first_payload = payloads[0] if payloads else {}
    channel_name = first_payload.get("source_channel_name") or "unknown"
    channel_id = first_payload.get("source_channel_id") or "unknown"
    message_ids = ", ".join(_signal_message_id(sig) for sig in batch)
    times = [str(_row_get(sig, "received_at")) for sig in batch]
    if len(times) == 1:
        time_range = times[0]
    else:
        time_range = f"{times[0]} - {times[-1]}"

    blocks = []
    media_paths: list[str] = []
    for sig, payload in zip(batch, payloads):
        text = (payload.get("raw_text") or "").strip() or "(无文字,仅图片)"
        blocks.append(
            f"- 消息ID: {_signal_message_id(sig)}\n"
            f"  signal_id: {_row_get(sig, 'signal_id')}\n"
            f"  时间: {_row_get(sig, 'received_at')}\n"
            f"  正文:\n{text}"
        )
        media_paths.extend(_copy_media(payload))
    media_block = ""
    if media_paths:
        listing = "\n".join(f"  {p}" for p in media_paths)
        media_block = f"图片附件(用 image 工具读取,注意用配置的 vision 模型):\n{listing}\n"
    context = _context_tail(channel_id, channel_name)
    return sanitize_prompt(PROMPT_TEMPLATE.format(
        channel_name=channel_name,
        channel_id=channel_id,
        message_ids=message_ids,
        time_range=time_range,
        channel_context=context,
        message_block="\n\n".join(blocks),
        media_block=media_block,
        signal_id=_row_get(batch[-1], "signal_id"),
    ))


def notify_blocked(sig: Any, dry_run: bool, reason: str | None = None) -> None:
    """A dropped message must never be silent: the user has to know the system
    did NOT act on it (a real Titan BTC signal was silently skipped on 07-04)."""
    try:
        payload = _payload(sig)
        alert_reason = reason
        if not alert_reason:
            alert_reason = (
                f"连续 {MAX_ATTEMPTS} 次投递失败"
                "(内容触发安全防护或投递异常),已跳过"
            )
        prompt = BLOCKED_NOTICE_TEMPLATE.format(
            channel_name=payload.get("source_channel_name") or "unknown",
            message_id=payload.get("source_message_id") or _row_get(sig, "signal_id"),
            reason=alert_reason,
        )
        run_hermes(sanitize_prompt(prompt), name=f"blocked-{_row_get(sig, 'signal_id')}", dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 - notification is best-effort
        log(f"blocked-notice failed for {_row_get(sig, 'signal_id')}: {exc}")


def run_hermes(prompt: str, name: str, dry_run: bool) -> str | None:
    schedule = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    create_cmd = [
        HERMES_BIN, "cron", "create", schedule, prompt,
        "--name", name, "--deliver", "telegram", "--repeat", "1",
        "--skill", "v3-trader",
    ]
    if dry_run:
        log(f"DRY-RUN would exec: {' '.join(create_cmd[:4])} <prompt {len(prompt)} chars> "
            f"--name {name} --deliver telegram --repeat 1 --skill v3-trader ; then cron run <job>")
        return "dry-run"
    out = subprocess.run(create_cmd, env=HERMES_ENV, capture_output=True, text=True, timeout=60)
    match = re.search(r"Created job:\s*(\S+)", out.stdout or "")
    if out.returncode != 0 or not match:
        log(f"cron create failed rc={out.returncode} stdout={out.stdout[-300:]!r} stderr={out.stderr[-300:]!r}")
        return None
    job_id = match.group(1)
    run = subprocess.run(
        [HERMES_BIN, "cron", "run", job_id, "--accept-hooks"],
        env=HERMES_ENV, capture_output=True, text=True, timeout=RUN_TIMEOUT,
    )
    if run.returncode != 0:
        log(f"cron run {job_id} failed rc={run.returncode} stderr={run.stderr[-300:]!r}")
        return None
    log(f"handled via job {job_id}: {(run.stdout or '')[-160:]!r}")
    return job_id


def _job_id_from_record(record: Any, name: str) -> str | None:
    if not isinstance(record, dict):
        return None
    record_name = str(record.get("name") or record.get("job_name") or "")
    if record_name != name:
        return None
    job_id = record.get("id")
    if not job_id:
        job_id = record.get("job_id")
    if not job_id:
        job_id = record.get("jobId")
    if not job_id:
        return None
    return str(job_id)


def _find_job_in_json(data: Any, name: str) -> str | None:
    if isinstance(data, list):
        for item in data:
            job_id = _job_id_from_record(item, name)
            if job_id:
                return job_id
        return None
    if not isinstance(data, dict):
        return None
    direct = _job_id_from_record(data, name)
    if direct:
        return direct
    for key in ("jobs", "items", "data", "results"):
        if key not in data:
            continue
        job_id = _find_job_in_json(data.get(key), name)
        if job_id:
            return job_id
    return None


def _find_job_in_text(text: str, name: str) -> str | None:
    name_pattern = re.compile(rf"(?<!\S){re.escape(name)}(?!\S)")
    for line in str(text or "").splitlines():
        if not name_pattern.search(line):
            continue
        uuid_match = re.search(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}\b",
            line,
            flags=re.IGNORECASE,
        )
        if uuid_match:
            return uuid_match.group(0)
        tokens = [
            token.strip("│|[](),")
            for token in line.split()
            if token.strip("│|[](),")
        ]
        for token in tokens:
            if token == name:
                continue
            if re.fullmatch(r"[A-Za-z0-9_-]{6,}", token):
                return token
    return None


def find_existing_cron_job(name: str) -> str | None | bool:
    """Return an exact-name Hermes job, None when absent, False on query error."""
    try:
        out = subprocess.run(
            [HERMES_BIN, "cron", "list"],
            env=HERMES_ENV,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"cron job lookup failed for {name}: {exc!r}")
        return False
    if out.returncode != 0:
        log(
            f"cron job lookup failed for {name}: rc={out.returncode} "
            f"stderr={out.stderr[-300:]!r}"
        )
        return False
    stdout = out.stdout or ""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return _find_job_in_text(stdout, name)
    return _find_job_in_json(data, name)


def _latest_markdown_response(job_id: str) -> str | None:
    job_dir = os.path.join(HERMES_OUTPUT_DIR, job_id)
    try:
        files = [
            os.path.join(job_dir, name)
            for name in os.listdir(job_dir)
            if name.endswith(".md")
        ]
    except FileNotFoundError:
        return None
    if not files:
        return None
    latest = max(files, key=lambda path: os.path.getmtime(path))
    try:
        data = open(latest).read()
    except OSError:
        return None
    match = re.search(r"(?ms)^## Response\s*\n(.*?)(?:\n##\s+|\Z)", data)
    if not match:
        return None
    response = match.group(1).strip()
    return response or None


def capture_hermes_response(job_id: str, sleep_func=time.sleep) -> str:
    if not job_id or job_id == "dry-run":
        return "(结果未捕获)"
    deadline = time.monotonic() + RESPONSE_CAPTURE_SECONDS
    while True:
        response = _latest_markdown_response(job_id)
        if response is not None:
            return response
        if time.monotonic() >= deadline:
            return "(结果未捕获)"
        sleep_func(2)


def append_channel_context(batch: list[Any], response: str, now: datetime | None = None) -> None:
    payload = _payload(batch[0])
    channel_id = payload.get("source_channel_id") or "unknown"
    channel_name = payload.get("source_channel_name") or "unknown"
    path = channel_context_path(channel_id, channel_name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    stamp = _coerce_now(now).isoformat()
    msg_ids = ",".join(_signal_message_id(sig) for sig in batch)
    summary = _message_summary(batch, limit=200)
    compact_response = " ".join(str(response).split())[:300]
    line = f"- {stamp} | msg {msg_ids} | 摘要: {summary} | 回复: {compact_response}\n"
    if not os.path.exists(path):
        with open(path, "w") as fh:
            fh.write(f"# Channel context: {channel_name} ({channel_id})\n")
    with open(path, "a") as fh:
        fh.write(line)


def is_brain_failure(response: str) -> bool:
    text = str(response or "")
    return any(marker in text for marker in BRAIN_FAILURE_MARKERS)


def deliver_batch(
    batch: list[Any],
    dry_run: bool,
    now_func=time.time,
    sleep_func=time.sleep,
    existing_job_id: str | None = None,
) -> bool:
    if not batch:
        return True
    prompt = build_prompt(batch)
    first_id = _row_get(batch[0], "signal_id")
    last_id = _row_get(batch[-1], "signal_id")
    name = f"signal-{first_id}" if first_id == last_id else f"signal-{first_id}-{last_id}"
    job_id = existing_job_id
    if job_id:
        log(f"reusing existing Hermes job {job_id} for {name}")
    else:
        job_id = run_hermes(prompt, name=name, dry_run=dry_run)
    if not job_id:
        return False
    if not dry_run:
        response = capture_hermes_response(str(job_id), sleep_func=sleep_func)
        if is_brain_failure(response):
            log(f"brain failure for job {job_id}: {response[:120]!r}; will retry batch")
            return False
        append_channel_context(batch, response, now=_coerce_now(now_func()))
    return True


def _batch_job_name(batch: list[Any]) -> str:
    first_id = _row_get(batch[0], "signal_id")
    last_id = _row_get(batch[-1], "signal_id")
    if first_id == last_id:
        return f"signal-{first_id}"
    return f"signal-{first_id}-{last_id}"


def _failed_batch_attempt(
    batch: list[Any],
    key: str,
    dry_run: bool,
    attempts: dict[str, int],
    retry_after: dict[str, float],
    timeout_keys: set[str],
    now_ts: float,
) -> str:
    attempt = attempts.get(key, 0) + 1
    attempts[key] = attempt
    if attempt < MAX_ATTEMPTS:
        delay = BRAIN_RETRY_DELAY_SECONDS * attempt
        retry_after[key] = now_ts + delay
        log(
            f"attempt {attempt}/{MAX_ATTEMPTS} failed for {key}; "
            f"retry after {int(delay)}s"
        )
        return "retry"
    log(f"SKIPPING poison batch starting {key} after {MAX_ATTEMPTS} attempts")
    reason = (
        f"连续 {MAX_ATTEMPTS} 次投递失败,已跳过。"
        "其中发生本地 Hermes 超时；远端 cron job 可能仍在运行，存在双 job 风险。"
    )
    if key not in timeout_keys:
        reason = (
            f"连续 {MAX_ATTEMPTS} 次投递失败"
            "(内容触发安全防护或投递异常),已跳过"
        )
    for sig in batch:
        notify_blocked(sig, dry_run=dry_run, reason=reason)
    attempts.pop(key, None)
    retry_after.pop(key, None)
    timeout_keys.discard(key)
    return "skip"


def attempt_batch_delivery(
    batch: list[Any],
    dry_run: bool,
    attempts: dict[str, int],
    retry_after: dict[str, float],
    timeout_keys: set[str],
    now_ts: float | None = None,
) -> str:
    """Try one batch and return success, retry, or skip.

    A prior local timeout makes the cron create/run outcome uncertain. The
    exact signal job name is queried before retrying so an existing remote job
    is reused. A confirmed absence permits a new dispatch.
    """
    if now_ts is None:
        now_ts = time.time()
    key = _signal_cursor(batch[0])
    existing_job_id = None
    if key in timeout_keys:
        name = _batch_job_name(batch)
        lookup = find_existing_cron_job(name)
        if lookup is False:
            log(
                f"retry deferred for {key}: cron job lookup unavailable; "
                "remote execution state is uncertain and has 双 job 风险"
            )
            return _failed_batch_attempt(
                batch,
                key,
                dry_run,
                attempts,
                retry_after,
                timeout_keys,
                now_ts,
            )
        if lookup:
            existing_job_id = str(lookup)
        else:
            log(
                f"no existing cron job found for {key}; redispatching with "
                "双 job 风险 because the prior local timeout may have hidden creation"
            )
    try:
        ok = deliver_batch(
            batch,
            dry_run=dry_run,
            existing_job_id=existing_job_id,
        )
    except subprocess.TimeoutExpired as exc:
        timeout_keys.add(key)
        log(
            f"hermes invocation timed out for {key}: {exc}; attempt counts as "
            "failed and remote cron may still be running (双 job 风险)"
        )
        return _failed_batch_attempt(
            batch,
            key,
            dry_run,
            attempts,
            retry_after,
            timeout_keys,
            now_ts,
        )
    if not ok:
        return _failed_batch_attempt(
            batch,
            key,
            dry_run,
            attempts,
            retry_after,
            timeout_keys,
            now_ts,
        )
    attempts.pop(key, None)
    retry_after.pop(key, None)
    timeout_keys.discard(key)
    return "success"


def compress_channel_contexts(now_func=time.time) -> None:
    try:
        names = os.listdir(CHANNEL_CONTEXT_DIR)
    except FileNotFoundError:
        return
    now_ts = float(now_func())
    for name in names:
        if not name.endswith(".md"):
            continue
        path = os.path.join(CHANNEL_CONTEXT_DIR, name)
        try:
            stat = os.stat(path)
            data = open(path).read()
        except OSError:
            continue
        if len(data) <= CONTEXT_COMPRESS_CHARS:
            continue
        if now_ts - stat.st_mtime < CONTEXT_IDLE_SECONDS:
            continue
        entries = [line for line in data.splitlines() if line.startswith("- ")]
        if len(entries) <= CONTEXT_KEEP_ENTRIES:
            continue
        older = entries[:-CONTEXT_KEEP_ENTRIES]
        recent = entries[-CONTEXT_KEEP_ENTRIES:]
        start = older[0][2:].split("|", 1)[0].strip()
        end = older[-1][2:].split("|", 1)[0].strip()
        header = f"# Channel context\n更早 {len(older)} 条已归档(起止时间 {start} 到 {end})\n"
        with open(path, "w") as fh:
            fh.write(header + "\n".join(recent) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print planned hermes commands; no cursor writes")
    ap.add_argument("--once", action="store_true", help="single poll pass, then exit")
    args = ap.parse_args()

    lock_fh = open(LOCK, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another feeder instance is running; exiting", file=sys.stderr)
        sys.exit(1)

    cursor = load_cursor()
    attempts: dict[str, int] = {}
    retry_after: dict[str, float] = {}
    timeout_keys: set[str] = set()
    log(f"feeder start cursor={cursor!r} dry_run={args.dry_run}")

    while True:
        try:
            if not args.dry_run:
                compress_channel_contexts()
            conn = _connect()
            try:
                if cursor is None:
                    cursor = latest_cursor(conn)
                    log(f"initialized cursor to latest={cursor!r} (history is not replayed)")
                    if cursor and not args.dry_run:
                        save_cursor(cursor)
                while True:
                    rows = fetch_new(conn, cursor)
                    batch = select_deliverable_batch(rows)
                    if not batch:
                        break
                    key = _signal_cursor(batch[0])
                    if time.time() < retry_after.get(key, 0):
                        break  # backoff window after a brain failure
                    result = attempt_batch_delivery(
                        batch,
                        dry_run=args.dry_run,
                        attempts=attempts,
                        retry_after=retry_after,
                        timeout_keys=timeout_keys,
                    )
                    if result == "retry":
                        break
                    cursor = _signal_cursor(batch[-1])
                    if not args.dry_run:
                        save_cursor(cursor)
            finally:
                conn.close()
        except sqlite3.OperationalError as exc:
            log(f"watcher db unavailable: {exc}")
        except subprocess.TimeoutExpired as exc:
            log(f"unexpected subprocess timeout outside batch accounting: {exc}")
        except Exception as exc:  # noqa: BLE001 - a poison row must not kill all channels
            log(f"UNEXPECTED feeder error (continuing): {exc!r}")
        if args.once:
            break
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
