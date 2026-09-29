"""wac-100: local test backend -- seed generator, env file, teardown.

No sockets are bound here; the end-to-end up/verify/down run is documented in
scripts/dev/wac_local_backend/README.md.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PKG = REPO / "scripts/dev/wac_local_backend"
WATCHER = REPO / "bridge/services/telegram-watcher"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


seed = _load("seed", PKG / "seed.py")
backend = _load("wac_local_backend_manager", PKG / "backend.py")
routes = _load("wac_generated_gateway_routes", REPO / "services/control-plane/api/generated/watcher_gateway_routes.py")

NOW = datetime.now(timezone.utc).replace(microsecond=0)
PAGE_SQL = (
    "SELECT * FROM {table} WHERE created_at >= datetime('now', '-' || ? || ' hours')"
    "{channel}{cursor} ORDER BY created_at DESC, id DESC LIMIT ?"
)


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    root = tmp_path_factory.mktemp("seed")
    result = seed.generate(root / "watcher-trading.db", root / "media", now=NOW)
    conn = sqlite3.connect(root / "watcher-trading.db")
    conn.row_factory = sqlite3.Row
    yield result, conn, root
    conn.close()


def _page(conn, table, hours, limit, channel=None, cursor=None):
    params = [hours]
    channel_sql = cursor_sql = ""
    if channel is not None:
        channel_sql = " AND channel_id = ?"
        params.append(channel)
    if cursor is not None:
        cursor_sql = " AND (created_at, id) < (?, ?)"
        params += list(cursor)
    params.append(limit)
    return conn.execute(PAGE_SQL.format(table=table, channel=channel_sql, cursor=cursor_sql), params).fetchall()


def _page_all(conn, table, hours, limit, channel=None):
    rows, cursor, boundaries = [], None, []
    while True:
        page = _page(conn, table, hours, limit, channel, cursor)
        if rows and page:
            boundaries.append(rows[-1]["created_at"] == page[0]["created_at"])
        rows += page
        if len(page) < limit:
            return rows, boundaries
        cursor = (page[-1]["created_at"], page[-1]["id"])


# ----------------------------------------------------------------------------- seed: counts and windows


def test_seed_counts_channels_and_window_split(seeded):
    result, conn, _ = seeded
    total = conn.execute("SELECT count(*) FROM telegram_messages").fetchone()[0]
    assert total == result.messages_total >= 2000
    channels = {row[0]: row[1] for row in conn.execute("SELECT channel_id, count(*) FROM telegram_messages GROUP BY channel_id")}
    assert len(channels) == 3 and min(channels.values()) >= 300
    window_start = NOW - timedelta(hours=24)
    stamps = [seed.parse_ts(row[0]) for row in conn.execute("SELECT created_at FROM telegram_messages")]
    inside = [s for s in stamps if s > window_start]
    outside = [s for s in stamps if s <= window_start]
    assert len(inside) == result.messages_in_window == 1800
    assert len(outside) == result.messages_out_window == 300
    # Margins: in-window rows stay in the window for an hour; out-of-window rows survive the watcher's 7-day purge.
    assert min(inside) >= NOW - timedelta(hours=23) and max(inside) <= NOW - timedelta(seconds=60)
    assert max(outside) <= NOW - timedelta(hours=25) and min(outside) > NOW - timedelta(days=7) + timedelta(hours=1)
    by_channel = {row[0]: row[1] for row in conn.execute(
        "SELECT channel_id, count(*) FROM telegram_messages WHERE created_at > ? GROUP BY channel_id", (seed.fmt_ts(window_start),))}
    assert by_channel == result.messages_in_window_by_channel


def test_seed_timestamps_use_watcher_storage_format_and_ties_exist(seeded):
    result, conn, _ = seeded
    fmt = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
    for table in ("telegram_messages", "briefings", "active_orders"):
        assert all(fmt.fullmatch(row[0]) for row in conn.execute(f"SELECT created_at FROM {table}"))
    tied = conn.execute(
        "SELECT count(*) FROM (SELECT created_at FROM telegram_messages GROUP BY created_at HAVING count(*) > 1)").fetchone()[0]
    assert tied >= 50 and result.tied_message_rows > tied
    albums = conn.execute(
        "SELECT count(*) FROM (SELECT channel_id, created_at FROM telegram_messages WHERE has_media = 1"
        " GROUP BY channel_id, created_at HAVING count(*) >= 3)").fetchone()[0]
    assert albums >= 10


def test_seed_text_mix_long_emoji_cjk(seeded):
    _, conn, _ = seeded
    texts = [row[0] for row in conn.execute("SELECT text FROM telegram_messages")]
    assert sum(len(t) > 1500 for t in texts) >= 100
    assert any("👨‍👩‍👧" in t for t in texts)
    assert sum(bool(re.search(r"[一-鿿]", t)) for t in texts) >= 1000
    assert sum(bool(re.search(r"[\U0001F300-\U0001FAFF]", t)) for t in texts) >= 500
    assert any(t == "" for t in texts)  # media-only album members


def test_composite_cursor_pagination_covers_window_exactly_once(seeded):
    result, conn, _ = seeded
    split_ties = False
    for limit in (37, 97, 200, 500):
        rows, boundaries = _page_all(conn, "telegram_messages", 24, limit)
        keys = [(row["created_at"], row["id"]) for row in rows]
        assert len(keys) == len(set(keys)) == result.messages_in_window
        assert keys == sorted(keys, reverse=True)
        split_ties |= any(boundaries)
    assert split_ties, "no page boundary fell inside a tie group; the cursor tie-break is untested"
    for channel_id, count in result.messages_in_window_by_channel.items():
        rows, _ = _page_all(conn, "telegram_messages", 24, 61, channel=channel_id)
        assert len(rows) == count
    rows, _ = _page_all(conn, "telegram_messages", 168, 500)
    assert len(rows) == result.messages_total
    rows, _ = _page_all(conn, "briefings", 24, 17)
    assert len(rows) == len({row["id"] for row in rows}) == result.briefings_in_window
    assert result.briefings_total > result.briefings_in_window


# ----------------------------------------------------------------------------- seed: media


def test_media_tiers_sizes_and_png_validity(seeded):
    result, conn, root = seeded
    by_tier = {}
    for item in result.media:
        by_tier.setdefault(item["tier"], []).append(item)
        data = (root / "media" / item["filename"]).read_bytes()
        assert len(data) == item["size"]
        assert seed.png_dimensions(data) == (item["width"], item["height"])
        low, high = seed.TIER_BOUNDS[item["tier"]]
        assert low <= len(data) <= high, item
    assert len(by_tier["small"]) >= 50 and all(i["size"] < seed.APP_THUMB_MAX_BYTES for i in by_tier["small"])
    assert len(by_tier["large"]) >= 5 and all(1.8 * seed.MIB <= i["size"] <= 2.3 * seed.MIB for i in by_tier["large"])
    huge = [i["size"] for i in by_tier["huge"]]
    assert len(huge) >= 2 and all(5 * seed.MIB < s < seed.CONTRACT_MAX_FILE_BYTES for s in huge)
    assert any(s < seed.APP_DETAIL_MAX_BYTES for s in huge) and any(s > seed.APP_DETAIL_MAX_BYTES for s in huge)
    assert len(by_tier["over_limit"]) >= 1 and all(i["size"] > seed.CONTRACT_MAX_FILE_BYTES for i in by_tier["over_limit"])
    assert seed.CONTRACT_MAX_FILE_BYTES == routes.PAYLOAD["budgets"]["media"]["max_file_bytes"]


def test_media_rows_reference_files_and_names_pass_gateway_pattern(seeded):
    result, conn, root = seeded
    pattern = re.compile(routes.PAYLOAD["path_params"]["filename"]["gateway_pattern"])
    rows = conn.execute("SELECT has_media, media_type, media_filename FROM telegram_messages WHERE has_media = 1").fetchall()
    names = [row["media_filename"] for row in rows]
    assert len(names) == len(set(names)) == len(result.media) + len(result.missing_media)
    assert all(row["media_type"] == "photo" for row in rows)
    on_disk = {p.name for p in (root / "media").iterdir()}
    assert on_disk == {item["filename"] for item in result.media}
    assert result.missing_media and not (set(result.missing_media) & on_disk)
    assert all(pattern.fullmatch(name) for name in names)
    assert conn.execute("SELECT count(*) FROM telegram_messages WHERE has_media = 0 AND media_filename != ''").fetchone()[0] == 0
    # Every tier is present on the first 24h page so the tester sees it without scrolling far.
    first_page = {row["media_filename"] for row in _page(conn, "telegram_messages", 24, 500)}
    tiers_on_first_page = {item["tier"] for item in result.media if item["filename"] in first_page}
    assert tiers_on_first_page >= {"large", "huge", "over_limit"}
    assert set(result.missing_media) & first_page


def test_seed_is_deterministic_and_refuses_existing_db(tmp_path):
    spec = seed.SeedSpec(messages_in_window=150, messages_out_window=20, briefings_in_window=10, briefings_out_window=2,
                         small_media=20, large_media=1, huge_media=0, over_limit_media=0, missing_media=1, album_groups=3)
    first = seed.generate(tmp_path / "a.db", tmp_path / "ma", now=NOW, spec=spec)
    second = seed.generate(tmp_path / "b.db", tmp_path / "mb", now=NOW, spec=spec)
    assert first.media == second.media and first.messages_in_window_by_channel == second.messages_in_window_by_channel
    with pytest.raises(FileExistsError):
        seed.generate(tmp_path / "a.db", tmp_path / "ma", now=NOW, spec=spec)


def _watcher_columns(table: str) -> list[str]:
    source = (WATCHER / "lib/trading-api.js").read_text(encoding="utf-8")
    match = re.search(r"CREATE TABLE IF NOT EXISTS " + table + r" \((.*?)\n\s*\)", source, re.S)
    assert match, table
    columns = []
    for line in match.group(1).splitlines():
        line = line.strip().rstrip(",")
        if line and not line.upper().startswith(("FOREIGN", "CHECK", "UNIQUE", "PRIMARY KEY (", ")", "OR ", "RISK_")):
            columns.append(line.split()[0])
    return columns


@pytest.mark.parametrize("table", ["telegram_messages", "briefings", "active_orders"])
def test_seed_ddl_matches_watcher_source(seeded, table):
    _, conn, _ = seeded
    seeded_columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    assert seeded_columns == _watcher_columns(table)
    assert " ".join(seed.DDL[table].split()) in " ".join((WATCHER / "lib/trading-api.js").read_text(encoding="utf-8").split())


# ----------------------------------------------------------------------------- env file and tokens


def test_tokens_are_fresh_distinct_and_watcher_valid():
    first, second = backend.generate_tokens(), backend.generate_tokens()
    assert set(first) == set(backend.ALL_TOKENS)
    assert len(set(first.values())) == len(first)
    assert not set(first.values()) & set(second.values())
    for value in first.values():
        assert re.fullmatch(r"[\x21-\x7E]{32,}", value)


def test_env_file_is_0600_exclusive_and_read_back(tmp_path):
    tokens = backend.generate_tokens()
    path = backend.write_env_file(tmp_path / "test.env", tokens)
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600
    assert backend.read_env_file(path) == tokens
    with pytest.raises(FileExistsError):
        backend.write_env_file(path, tokens)
    os.chmod(path, 0o644)
    with pytest.raises(backend.BackendError, match="permissions too open"):
        backend.read_env_file(path)


def test_env_file_rejects_unsafe_values(tmp_path):
    with pytest.raises(backend.BackendError):
        backend.write_env_file(tmp_path / "x.env", {"A": "value with space"})
    with pytest.raises(backend.BackendError):
        backend.write_env_file(tmp_path / "y.env", {"A": "ok\nB=injected"})
    assert not (tmp_path / "x.env").exists() and not (tmp_path / "y.env").exists()


def test_state_dir_layout_is_private(tmp_path):
    state = backend.init_state_dir(tmp_path / "state", {"port": 18999})
    assert stat.S_IMODE(os.stat(state).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(state / "secrets").st_mode) == 0o700
    assert backend.read_marker(state)["port"] == 18999
    with pytest.raises(backend.BackendError, match="already exists"):
        backend.init_state_dir(state, {"port": 18999})


def test_child_envs_are_built_from_scratch(monkeypatch, tmp_path):
    for name, value in {"RISK_ADMIN_TOKEN": "caller-real-looking-risk-admin-token-000000",
                        "DATABASE_URL": "postgresql://caller/should-not-leak",
                        "WATCHER_ALERT_BOT_TOKEN": "caller-bot-token", "SOME_CALLER_VAR": "x",
                        "TELEGRAM_PROXY_HOST": "10.0.0.1"}.items():
        monkeypatch.setenv(name, value)
    tokens = backend.generate_tokens()
    w = backend.watcher_env(tmp_path, tokens, "/opt/node/bin/node")
    g = backend.gateway_env(tmp_path, tokens, "/opt/py/bin/python", "operator-query")
    for env in (w, g):
        assert "DATABASE_URL" not in env and "SOME_CALLER_VAR" not in env and "WATCHER_ALERT_BOT_TOKEN" not in env
        assert "caller" not in json.dumps(env)
        assert env["HOME"] == str(tmp_path / "home")
    assert g["RISK_ADMIN_TOKEN"] == tokens["RISK_ADMIN_TOKEN"]
    assert g["WATCHER_GATEWAY_URL"] == "http://127.0.0.1:9100" and g["CONTROL_PLANE_APP_ROLE"] == "operator-query"
    assert g["WATCHER_GATEWAY_TOKEN"] == w["WATCHER_GATEWAY_TOKEN"]
    assert not {"WATCHER_SNAPSHOT_TOKEN", "WATCHER_BROWSER_PROXY_TOKEN"} & set(g)
    assert not set(backend.READER_TOKENS.values()) & set(w)
    assert w["WATCHER_HOST"] == "127.0.0.1" and w["PRICE_MONITOR_ENABLED"] == "0" and w["SIGNAL_IMPORTER_ENABLED"] == "0"
    assert w["TELEGRAM_PROXY_HOST"] == "127.0.0.1" and w["TELEGRAM_PROXY_PORT"] == "9"
    assert w["TRADER_TRADING_DB_PATH"] == w["TRADING_DB_PATH"] == g["TRADER_TRADING_DB_PATH"]
    assert str(w["TRADER_TRADING_DB_PATH"]).startswith(str(tmp_path))
    assert w["WATCHER_MEDIA_DIR"] == str(tmp_path / "media")


def test_instructions_never_contain_token_values(tmp_path):
    tokens = backend.generate_tokens()
    meta = {"port": 18731, "role": "operator-query", "state_dir": str(tmp_path)}
    text = backend.render_instructions(meta, tmp_path / "secrets/test.env", None)
    assert "adb -s <serial> reverse tcp:18731 tcp:18731" in text
    assert "http://127.0.0.1:18731 " in text
    assert not any(value in text for value in tokens.values())


def test_token_command_hides_value_unless_revealed(tmp_path, capsys):
    state = backend.init_state_dir(tmp_path / "state", {"port": 18998})
    tokens = backend.generate_tokens()
    backend.write_env_file(state / "secrets" / backend.ENV_NAME, tokens)
    assert backend.main(["token", "--state-dir", str(state)]) == 0
    out = capsys.readouterr().out
    assert tokens["VIEWER_TOKEN"] not in out and backend.token_fingerprint(tokens["VIEWER_TOKEN"]) in out
    assert backend.main(["token", "--state-dir", str(state), "--role", "risk_admin", "--reveal"]) == 0
    assert capsys.readouterr().out.strip() == tokens["RISK_ADMIN_TOKEN"]


def test_log_leak_scan_finds_token_values(tmp_path):
    state = backend.init_state_dir(tmp_path / "state", {"port": 18997})
    tokens = backend.generate_tokens()
    (state / "logs/gateway.log").write_text("INFO GET /v1/watcher/status 200\n")
    assert backend.logs_leak_tokens(state, tokens) == []
    # Non-secret env entries (base URL, role) legitimately appear in logs and are not leaks.
    base_url = "http://127.0.0.1:18997"
    (state / "logs/gateway.log").write_text(f"INFO Uvicorn running on {base_url}\n")
    assert backend.logs_leak_tokens(state, {**tokens, "WAC_BASE_URL": base_url}) == []
    (state / "logs/watcher.log").write_text("oops " + tokens["WATCHER_GATEWAY_TOKEN"])
    assert backend.logs_leak_tokens(state, tokens) == ["watcher.log:WATCHER_GATEWAY_TOKEN"]
    assert tokens["WATCHER_GATEWAY_TOKEN"] not in backend.log_tail(state / "logs/watcher.log", tokens)


# ----------------------------------------------------------------------------- down / teardown with stand-in processes


SLEEPER = "import sys, time; time.sleep(300)"
PARENT_WITH_CHILD = (
    "import subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)', sys.argv[1], 'child'])\n"
    "time.sleep(300)\n"
)


def _spawn(code: str, state: Path) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", code, str(state)], start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait_group(pgid: int, marker: str, count: int) -> list[int]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        members = backend._group_members(pgid, marker)
        if len(members) >= count:
            return members
        time.sleep(0.05)
    raise AssertionError("stand-in process group did not start")


def test_teardown_kills_recorded_groups_and_removes_state_dir(tmp_path):
    state = backend.init_state_dir((tmp_path / "state").resolve(), {"port": 18996})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("must survive")
    os.symlink(outside, state / "watcher-app-node_modules-link")
    watcher = _spawn(PARENT_WITH_CHILD, state)
    gateway = _spawn(SLEEPER, state)
    try:
        backend.record_process(state, "watcher", watcher)
        backend.record_process(state, "gateway", gateway)
        members = _wait_group(watcher.pid, str(state), 2)
        assert len(members) == 2
        logs = []
        assert backend.teardown(state, log=logs.append) is True
        assert watcher.wait(timeout=5) is not None and gateway.wait(timeout=5) is not None
        for pid in members + [gateway.pid]:
            assert not backend.pid_alive(pid)
        assert not state.exists()
        assert (outside / "keep.txt").read_text() == "must survive"
        assert any("watcher: stopped" in line for line in logs) and any("gateway: stopped" in line for line in logs)
    finally:
        for proc in (watcher, gateway):
            if proc.poll() is None:
                proc.kill()


def test_teardown_leaves_foreign_process_alone(tmp_path):
    state = backend.init_state_dir((tmp_path / "state").resolve(), {"port": 18995})
    foreign = subprocess.Popen([sys.executable, "-c", SLEEPER], start_new_session=True)  # no state dir on its command line
    try:
        backend.record_process(state, "watcher", foreign)
        logs = []
        assert backend.teardown(state, log=logs.append) is True
        assert foreign.poll() is None, "a process without our state dir marker must not be signalled"
        assert any("not ours" in line for line in logs)
    finally:
        foreign.kill()
        foreign.wait(timeout=5)


def test_teardown_ignores_reused_pid_with_different_start_time(tmp_path):
    state = backend.init_state_dir((tmp_path / "state").resolve(), {"port": 18994})
    proc = _spawn(SLEEPER, state)
    try:
        entry = backend.record_process(state, "gateway", proc)
        path = state / "run/processes.json"
        path.write_text(json.dumps([{**entry, "start": "Thu Jan  1 00:00:00 1970", "pgid": 999999}]))
        assert backend.owned_pids(state, json.loads(path.read_text())[0]) == []
        assert backend.teardown(state, log=lambda _line: None) is True
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_teardown_refuses_dir_without_marker(tmp_path):
    victim = tmp_path / "not-ours"
    victim.mkdir()
    (victim / "file").write_text("x")
    with pytest.raises(backend.BackendError, match="marker missing"):
        backend.teardown(victim)
    assert (victim / "file").exists()


def test_down_without_state_dir_is_a_noop(tmp_path, capsys):
    assert backend.main(["down", "--state-dir", str(tmp_path / "absent")]) == 0
    assert "nothing to do" in capsys.readouterr().out


def test_wrapper_script_parses():
    assert subprocess.run(["bash", "-n", str(PKG / "wac_local_backend.sh")]).returncode == 0
