"""D-08 整号 Telegram Desktop 导入：频道过滤、白名单、无原文报告与可执行突变。"""
from __future__ import annotations

import hashlib
import inspect
import json
import pathlib
import shutil
import textwrap
from datetime import UTC, datetime

import polars as pl
import pytest

from quant_lab.data import harvest
from quant_lab.data.lake import Layout, read_quarantine
from quant_lab.data.sources import (
    CHANNEL_CHAT_TYPES,
    canonical_peer_id,
    scan_tdesktop_document,
)

FIX_SINGLE = pathlib.Path(__file__).parent / "fixtures" / "tdesktop_sample" / "AlphaSignals"

CH1 = 111000001
CH2 = 111000002
LEFT = 111000003
EMPTY = 111000004
MISSING_ID = 111000099
CH1_PEER = canonical_peer_id(CH1, "public_channel")
CH2_PEER = canonical_peer_id(CH2, "private_channel")
LEFT_PEER = canonical_peer_id(LEFT, "public_channel")
EMPTY_PEER = canonical_peer_id(EMPTY, "public_channel")
MISSING_PEER = canonical_peer_id(MISSING_ID, "channel")

PERSONAL_TEXT = "D08_PERSONAL_CHAT_TEXT_MUST_NOT_LEAK"
GROUP_TEXT = "D08_GROUP_TEXT_MUST_NOT_LEAK"
BOT_TEXT = "D08_BOT_TEXT_MUST_NOT_LEAK"
SAVED_TEXT = "D08_SAVED_MESSAGES_TEXT_MUST_NOT_LEAK"
CONTACT_NAME = "D08_CONTACT_NAME_MUST_NOT_LEAK"
PERSONAL_NAME = "D08_PERSONAL_FULL_NAME_MUST_NOT_LEAK"
PERSONAL_CHAT_NAME = "D08_PERSONAL_CHAT_NAME_MUST_NOT_LEAK"
GROUP_NAME = "D08_GROUP_NAME_MUST_NOT_LEAK"
BOT_NAME = "D08_BOT_NAME_MUST_NOT_LEAK"
SAVED_NAME = "D08_SAVED_NAME_MUST_NOT_LEAK"
PERSONAL_PHOTO = "D08_PERSONAL_MEDIA_MUST_NOT_LEAK.png"
PERSONAL_MEDIA_BYTES = b"personal-bytes"
CHANNEL1_TEXT = "D08_CH1_BTC_LONG_ENTRY_61000"
CHANNEL2_TEXT = "D08_CH2_ETH_SHORT_ENTRY_3300"
LEFT_TEXT = "D08_LEFT_CHANNEL_ORDINARY_IN_LAKE"
SINGLE_MIX_NAME = "D08_SINGLE_MIXED_NAME_MUST_NOT_APPEAR"
SECRETS = (
    PERSONAL_TEXT, GROUP_TEXT, BOT_TEXT, SAVED_TEXT, CONTACT_NAME, PERSONAL_NAME,
    PERSONAL_CHAT_NAME, GROUP_NAME, BOT_NAME, SAVED_NAME, PERSONAL_PHOTO,
)
NON_CHANNEL_TYPES = (
    "personal_chat", "private_group", "public_group", "private_supergroup", "public_supergroup", "bot_chat", "saved_messages",
)

TS_2024_A = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
TS_2024_B = int(datetime(2024, 1, 2, tzinfo=UTC).timestamp())
TS_2024_EDIT = int(datetime(2024, 1, 1, 1, tzinfo=UTC).timestamp())
TS_2025 = int(datetime(2025, 6, 1, tzinfo=UTC).timestamp())
PHOTO_REL = "chats/chat_001/photos/pic.png"
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def mutant(function, before, after):
    source = textwrap.dedent(inspect.getsource(function))
    assert source.count(before) == 1
    namespace = dict(function.__globals__)
    exec(compile(source.replace(before, after), "<account-mutant>", "exec"), namespace)
    return namespace[function.__name__]


def reset_lake(root: pathlib.Path) -> None:
    shutil.rmtree(root / "lake", ignore_errors=True)
    shutil.rmtree(root / "quarantine", ignore_errors=True)


def _msg(mid, *, text="", unix, edited=None, photo=None, reply=None, mtype="message", extra=None):
    rec = {
        "id": mid,
        "type": mtype,
        "date": datetime.fromtimestamp(unix, UTC).strftime("%Y-%m-%dT%H:%M:%S"),
        "date_unixtime": str(unix),
        "text": text,
        "text_entities": [],
    }
    if edited is not None:
        rec["edited"] = datetime.fromtimestamp(edited, UTC).strftime("%Y-%m-%dT%H:%M:%S")
        rec["edited_unixtime"] = str(edited)
    if photo is not None:
        rec["photo"] = photo
    if reply is not None:
        rec["reply_to_message_id"] = reply
    if extra:
        rec.update(extra)
    return rec


def build_account_doc():
    return {
        "personal_information": {"first_name": PERSONAL_NAME, "phone_number": "+10000000000"},
        "contacts": {"about": "contacts", "list": [{"first_name": CONTACT_NAME, "phone_number": "+1999"}]},
        "chats": {
            "about": "chats",
            "list": [
                {
                    "name": "Chan One",
                    "type": "public_channel",
                    "id": CH1,
                    "messages": [
                        _msg(1, text="", unix=TS_2024_A, mtype="service", extra={"action": "create_channel"}),
                        _msg(2, text=CHANNEL1_TEXT, unix=TS_2024_A, edited=TS_2024_EDIT, photo=PHOTO_REL),
                        _msg(3, text="follow-up", unix=TS_2024_B, reply=2),
                    ],
                },
                {
                    "name": "Chan Two",
                    "type": "private_channel",
                    "id": CH2,
                    "messages": [_msg(1, text=CHANNEL2_TEXT, unix=TS_2025)],
                },
                {"name": "Empty Channel", "type": "public_channel", "id": EMPTY, "messages": []},
                {
                    "name": PERSONAL_CHAT_NAME,
                    "type": "personal_chat",
                    "id": MISSING_ID,
                    "messages": [_msg(1, text=PERSONAL_TEXT, unix=TS_2024_A, photo=f"chats/chat_009/photos/{PERSONAL_PHOTO}")],
                },
                {
                    "name": GROUP_NAME,
                    "type": "private_group",
                    "id": 666,
                    "messages": [_msg(1, text=GROUP_TEXT, unix=TS_2024_A)],
                },
                {
                    "name": BOT_NAME,
                    "type": "bot_chat",
                    "id": 777,
                    "messages": [_msg(1, text=BOT_TEXT, unix=TS_2024_A)],
                },
                {
                    "name": SAVED_NAME,
                    "type": "saved_messages",
                    "id": 888,
                    "messages": [_msg(1, text=SAVED_TEXT, unix=TS_2024_A)],
                },
            ],
        },
        "left_chats": {
            "about": "left",
            "list": [{
                "name": "Left Channel",
                "type": "public_channel",
                "id": LEFT,
                "messages": [_msg(1, text=LEFT_TEXT, unix=TS_2024_A)],
            }],
        },
    }


def write_account(directory: pathlib.Path) -> pathlib.Path:
    directory.mkdir(parents=True, exist_ok=True)
    photo = directory / PHOTO_REL
    photo.parent.mkdir(parents=True, exist_ok=True)
    photo.write_bytes(PNG)
    personal_media = directory / "chats" / "chat_009" / "photos" / PERSONAL_PHOTO
    personal_media.parent.mkdir(parents=True, exist_ok=True)
    personal_media.write_bytes(PERSONAL_MEDIA_BYTES)
    (directory / "result.json").write_text(json.dumps(build_account_doc(), ensure_ascii=False), encoding="utf-8")
    return directory


@pytest.fixture
def account(lake):
    directory = write_account(lake / "import" / "telegram")
    return lake, directory


def output_text_and_bytes(root: pathlib.Path, report, stdout="", stderr="", log_text="") -> tuple[str, bytes]:
    texts = [json.dumps(report, ensure_ascii=False, default=str), stdout, stderr, log_text]
    blobs = [json.dumps(report, ensure_ascii=False, default=str).encode(), stdout.encode(), stderr.encode(), log_text.encode()]
    for folder in (root / "lake", root / "quarantine", root / "reports"):
        if not folder.exists():
            continue
        for path in folder.rglob("*"):
            if not path.is_file():
                continue
            raw = path.read_bytes()
            blobs.append(raw)
            if path.suffix == ".parquet":
                df = pl.read_parquet(path)
                texts.append(json.dumps(df.to_dicts(), ensure_ascii=False, default=str))
            else:
                texts.append(raw.decode("utf-8", "surrogateescape"))
    return "\n".join(texts), b"\n".join(blobs)


def assert_no_secrets(root, report, stdout="", stderr="", log_text=""):
    text, raw = output_text_and_bytes(root, report, stdout, stderr, log_text)
    for secret in SECRETS:
        assert secret not in text
        assert secret.encode() not in raw
    assert PERSONAL_MEDIA_BYTES not in raw
    assert not list((root / "lake").rglob("result.json")) if (root / "lake").exists() else True


def assert_ch1_metrics(report):
    row = next(r for r in report["channel_reports"] if r["id"] == CH1_PEER)
    assert row["n_messages"] == 2
    assert row["edit_ratio"] == 0.5
    assert row["image_ratio"] == 0.5
    assert row["reply_ratio"] == 0.5
    assert row["year_distribution"] == {"2024": 2}


def assert_whitelist_extensions(report):
    assert report["channel_reports"]
    assert any(row["id"] == CH1_PEER for row in report["channel_reports"])
    assert report["whitelist"]["missing"] == [MISSING_PEER]


class PoisonChat(dict):
    def __init__(self, *args, forbid_body=False, **kwargs):
        super().__init__(*args, **kwargs)
        self._typed = False
        self._forbid_body = forbid_body

    def get(self, key, default=None):
        if key == "type":
            self._typed = True
            return super().get(key, default)
        if key in ("messages", "name", "id", "photo", "file"):
            if not self._typed:
                raise AssertionError(f"{key} before type")
            if super().get("type") not in CHANNEL_CHAT_TYPES:
                raise AssertionError(f"{key} on non-channel")
            if self._forbid_body and key in ("messages", "name", "photo", "file"):
                raise AssertionError(f"{key} on unselected channel")
        return super().get(key, default)

    def __getitem__(self, key):
        value = self.get(key, _MISSING)
        if value is _MISSING:
            raise KeyError(key)
        return value


_MISSING = object()


class PoisonDoc:
    FORBIDDEN = ("personal_information", "contacts")

    def __init__(self, inner):
        self._inner = inner

    def get(self, key, default=None):
        if key in self.FORBIDDEN:
            raise AssertionError(f"must not read {key}")
        return self._inner.get(key, default)

    def __getitem__(self, key):
        if key in self.FORBIDDEN:
            raise AssertionError(f"must not read {key}")
        return self._inner[key]

    def __contains__(self, key):
        if key in self.FORBIDDEN:
            raise AssertionError(f"must not read {key}")
        return key in self._inner

    def __iter__(self):
        raise AssertionError("must not iterate top-level keys")

    def items(self):
        raise AssertionError("must not iterate top-level keys")

    def keys(self):
        raise AssertionError("must not iterate top-level keys")


def test_whole_account_ingests_channels_only(account, capsys, caplog):
    root, directory = account
    report_path = root / "reports" / "poc1a.json"
    assert harvest.main(["--report", str(report_path)]) == 0
    captured = capsys.readouterr()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["n_messages"] == 4
    assert report["n_service_messages"] == 1
    assert report["channels"] == sorted([CH1_PEER, CH2_PEER, LEFT_PEER, EMPTY_PEER])
    assert report["skipped_by_type"] == {"bot_chat": 1, "personal_chat": 1, "private_group": 1, "saved_messages": 1}
    assert report["whitelist"] == {"enabled": False, "ids": [], "missing": []}
    by_id = {row["id"]: row for row in report["channel_reports"]}
    assert len(report["channel_reports"]) == 4
    assert_ch1_metrics(report)
    assert by_id[CH1_PEER]["name"] == "Chan One"
    assert by_id[CH1_PEER]["start"] == datetime(2024, 1, 1, tzinfo=UTC).isoformat()
    assert by_id[CH1_PEER]["end"] == datetime(2024, 1, 2, tzinfo=UTC).isoformat()
    assert by_id[CH2_PEER]["name"] == "Chan Two"
    assert by_id[CH2_PEER]["n_messages"] == 1
    assert by_id[LEFT_PEER]["name"] == "Left Channel"
    assert by_id[LEFT_PEER]["n_messages"] == 1
    assert by_id[LEFT_PEER]["start"] == datetime(2024, 1, 1, tzinfo=UTC).isoformat()
    assert report["edit_ratio"] == pytest.approx(1 / 4)
    assert report["image_ratio"] == pytest.approx(1 / 4)
    assert report["reply_ratio"] == pytest.approx(1 / 4)
    assert report["year_distribution"] == {"2024": 3, "2025": 1}
    layout = Layout.from_root(root)
    bronze = pl.read_parquet(layout.message_version)
    assert set(bronze["channel_id"].to_list()) == {CH1_PEER, CH2_PEER, LEFT_PEER}
    assert {CHANNEL1_TEXT, CHANNEL2_TEXT, LEFT_TEXT} <= set(bronze["text"].to_list())
    assert hashlib.sha256((directory / "result.json").read_bytes()).hexdigest() in set(bronze["raw_hash"])
    media_names = {p.name for p in layout.media_dir.glob("*")} if layout.media_dir.exists() else set()
    assert hashlib.sha256(PNG).hexdigest() in "".join(media_names)
    assert PERSONAL_PHOTO not in media_names
    dumped = json.dumps(report, ensure_ascii=False)
    assert CHANNEL1_TEXT not in dumped and CHANNEL2_TEXT not in dumped and LEFT_TEXT not in dumped
    assert "follow-up" not in dumped
    assert_no_secrets(root, report, captured.out, captured.err, caplog.text)


def test_empty_channel_listed_not_in_bronze(account):
    root, _ = account
    report = harvest.run()
    row = next(r for r in report["channel_reports"] if r["id"] == EMPTY_PEER)
    assert row["name"] == "Empty Channel"
    assert row["n_messages"] == 0
    assert row["start"] is None and row["end"] is None
    bronze = pl.read_parquet(Layout.from_root(root).message_version)
    assert EMPTY_PEER not in set(bronze["channel_id"].to_list())


def test_chats_null_refuses_without_leaking(lake, capsys, caplog):
    directory = lake / "import" / "telegram"
    directory.mkdir(parents=True)
    doc = {
        "chats": None,
        "type": "personal_chat",
        "id": 555,
        "name": PERSONAL_CHAT_NAME,
        "messages": [_msg(1, text=PERSONAL_TEXT, unix=TS_2024_A)],
        "personal_information": {"first_name": PERSONAL_NAME},
    }
    (directory / "result.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    report_path = lake / "reports" / "x.json"
    assert harvest.main(["--report", str(report_path)]) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.err)["reason"] == "TDESKTOP_EXPORT_INVALID"
    haystack = captured.out + captured.err + caplog.text
    assert PERSONAL_TEXT not in haystack and PERSONAL_CHAT_NAME not in haystack and PERSONAL_NAME not in haystack
    assert not report_path.exists()
    assert not (lake / "lake").exists()


@pytest.mark.parametrize("chat_type", NON_CHANNEL_TYPES)
@pytest.mark.parametrize("left", [False, True])
def test_poison_non_channel_types(tmp_path, chat_type, left):
    channel = PoisonChat({"name": "Chan One", "type": "public_channel", "id": CH1, "messages": [_msg(1, text=CHANNEL1_TEXT, unix=TS_2024_A)]})
    other = PoisonChat({"name": "nope", "type": chat_type, "id": 1, "messages": [_msg(1, text=PERSONAL_TEXT, unix=TS_2024_A)]})
    inner = {"personal_information": {"first_name": PERSONAL_NAME}, "contacts": {"list": [{"first_name": CONTACT_NAME}]}}
    if left:
        inner["chats"] = {"list": [channel]}
        inner["left_chats"] = {"list": [other]}
    else:
        inner["chats"] = {"list": [channel, other]}
    scan = scan_tdesktop_document(PoisonDoc(inner), tmp_path, raw_uri="result.json", raw_hash="abc")
    assert [m.text for m in scan.messages] == [CHANNEL1_TEXT]
    assert scan.skipped_by_type == {chat_type: 1}


def test_poison_unselected_channel_skips_body(tmp_path):
    selected = PoisonChat({"name": "Chan One", "type": "public_channel", "id": CH1, "messages": [_msg(1, text=CHANNEL1_TEXT, unix=TS_2024_A)]})
    unselected = PoisonChat(
        {"name": "Chan Two", "type": "private_channel", "id": CH2, "messages": [_msg(1, text=CHANNEL2_TEXT, unix=TS_2025, photo=PHOTO_REL)]},
        forbid_body=True,
    )
    doc = PoisonDoc({"chats": {"list": [selected, unselected]}})
    scan = scan_tdesktop_document(doc, tmp_path, raw_uri="result.json", raw_hash="abc", allowed_peer_ids=frozenset({CH1_PEER}))
    assert [m.channel_id for m in scan.messages] == [CH1_PEER]
    present = {c.channel_id: c.ingested for c in scan.listings}
    assert present[CH1_PEER] is True and present[CH2_PEER] is False


def test_whitelist_two_id_formats_and_missing(account):
    root, _ = account
    (root / "import" / "telegram" / "channels.txt").write_text(
        f"# keep these\n{CH1}\n{CH2_PEER}\n{MISSING_ID}\n\n",
        encoding="utf-8",
    )
    report = harvest.run()
    assert report["whitelist"]["enabled"] is True
    assert report["whitelist"]["ids"] == sorted([MISSING_PEER, CH1_PEER, CH2_PEER])
    assert report["whitelist"]["missing"] == [MISSING_PEER]
    assert report["channels"] == sorted([CH1_PEER, CH2_PEER])
    assert LEFT_PEER not in report["channels"]
    assert report["n_messages"] == 3
    layout = Layout.from_root(root)
    bronze = pl.read_parquet(layout.message_version)
    assert set(bronze["channel_id"].to_list()) == {CH1_PEER, CH2_PEER}
    assert_no_secrets(root, report)


def test_whitelist_selects_left_chats(account):
    root, _ = account
    (root / "import" / "telegram" / "channels.txt").write_text(f"{LEFT}\n", encoding="utf-8")
    report = harvest.run()
    assert report["channels"] == [LEFT_PEER]
    assert report["n_messages"] == 1
    bronze = pl.read_parquet(Layout.from_root(root).message_version)
    assert set(bronze["channel_id"].to_list()) == {LEFT_PEER}
    assert LEFT_TEXT in bronze["text"].to_list()
    assert_no_secrets(root, report)


def test_empty_whitelist_imports_nothing(account):
    root, _ = account
    (root / "import" / "telegram" / "channels.txt").write_text("# nobody\n\n", encoding="utf-8")
    report = harvest.run()
    assert report["n_messages"] == 0
    assert report["whitelist"] == {"enabled": True, "ids": [], "missing": []}
    assert report["channels"] == []
    assert report["skipped_by_type"]["personal_chat"] == 1
    layout = Layout.from_root(root)
    bronze = pl.read_parquet(layout.message_version)
    assert bronze.height == 0
    text, raw = output_text_and_bytes(root, report)
    assert CHANNEL1_TEXT not in text and CHANNEL2_TEXT.encode() not in raw and LEFT_TEXT not in text
    assert_no_secrets(root, report)


def test_unmatched_whitelist_returns_missing(account):
    root, _ = account
    (root / "import" / "telegram" / "channels.txt").write_text(f"{MISSING_ID}\n", encoding="utf-8")
    report = harvest.run()
    assert report["n_messages"] == 0
    assert report["whitelist"]["missing"] == [MISSING_PEER]
    assert report["whitelist"]["ids"] == [MISSING_PEER]
    layout = Layout.from_root(root)
    assert pl.read_parquet(layout.message_version).height == 0
    assert_no_secrets(root, report)


def test_missing_across_exports_left_chats_and_nonchannel_id(lake):
    root = lake / "import" / "telegram"
    a = root / "exp_a"
    a.mkdir(parents=True)
    (a / "result.json").write_text(json.dumps({
        "chats": {"list": [{"name": "Chan One", "type": "public_channel", "id": CH1, "messages": [_msg(1, text=CHANNEL1_TEXT, unix=TS_2024_A)]}]},
    }, ensure_ascii=False), encoding="utf-8")
    b = root / "exp_b"
    b.mkdir(parents=True)
    (b / "result.json").write_text(json.dumps({
        "chats": {"list": [{
            "name": PERSONAL_CHAT_NAME, "type": "personal_chat", "id": MISSING_ID,
            "messages": [_msg(1, text=PERSONAL_TEXT, unix=TS_2024_A)],
        }]},
        "left_chats": {"list": [{"name": "Left Channel", "type": "public_channel", "id": LEFT, "messages": [_msg(1, text=LEFT_TEXT, unix=TS_2024_A)]}]},
    }, ensure_ascii=False), encoding="utf-8")
    (root / "channels.txt").write_text(f"{CH1}\n{LEFT}\n{MISSING_ID}\n{CH2}\n", encoding="utf-8")
    report = harvest.run()
    assert report["whitelist"]["missing"] == sorted([MISSING_PEER, CH2_PEER])
    assert report["channels"] == sorted([CH1_PEER, LEFT_PEER])
    bronze = pl.read_parquet(Layout.from_root(lake).message_version)
    assert set(bronze["channel_id"].to_list()) == {CH1_PEER, LEFT_PEER}
    assert_no_secrets(lake, report)


def test_export_dir_uses_data_root_whitelist(lake, tmp_path):
    export_dir = write_account(tmp_path / "export-elsewhere")
    whitelist_dir = lake / "import" / "telegram"
    whitelist_dir.mkdir(parents=True, exist_ok=True)
    (whitelist_dir / "channels.txt").write_text(f"{CH1}\n", encoding="utf-8")
    report = harvest.run(export_dir=export_dir)
    assert report["channels"] == [CH1_PEER]
    assert report["n_messages"] == 2
    assert report["whitelist"]["enabled"] is True
    layout = Layout.from_root(lake)
    assert set(pl.read_parquet(layout.message_version)["channel_id"].to_list()) == {CH1_PEER}
    assert_no_secrets(lake, report)


def test_single_chat_report_omits_name(lake):
    directory = lake / "import" / "telegram"
    shutil.copytree(FIX_SINGLE, directory)
    secret = "D08_SINGLE_CHAT_NAME_MUST_NOT_APPEAR"
    doc = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    doc["name"] = secret
    doc["messages"][0]["text"] = secret
    (directory / "result.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    report = harvest.run()
    assert report["whitelist"]["enabled"] is False
    assert all("name" not in row for row in report["channel_reports"])
    assert secret not in json.dumps(report, ensure_ascii=False)
    assert report["n_messages"] > 0


def test_mixed_single_and_account_hides_single_name(lake):
    base = lake / "import" / "telegram"
    single = base / "single"
    single.mkdir(parents=True)
    (single / "result.json").write_text(json.dumps({
        "name": SINGLE_MIX_NAME, "type": "private_channel", "id": CH2,
        "messages": [_msg(1, text=CHANNEL2_TEXT, unix=TS_2025)],
    }, ensure_ascii=False), encoding="utf-8")
    account_dir = base / "account"
    account_dir.mkdir(parents=True)
    (account_dir / "result.json").write_text(json.dumps({
        "chats": {"list": [
            {"name": "Chan One", "type": "public_channel", "id": CH1, "messages": [_msg(1, text=CHANNEL1_TEXT, unix=TS_2024_A)]},
            {"name": PERSONAL_CHAT_NAME, "type": "personal_chat", "id": 555, "messages": [_msg(1, text=PERSONAL_TEXT, unix=TS_2024_A)]},
        ]},
    }, ensure_ascii=False), encoding="utf-8")
    report = harvest.run()
    by_id = {row["id"]: row for row in report["channel_reports"]}
    assert by_id[CH1_PEER]["name"] == "Chan One"
    assert "name" not in by_id[CH2_PEER]
    assert SINGLE_MIX_NAME not in json.dumps(report, ensure_ascii=False)
    assert_no_secrets(lake, report)


def test_merge_same_channel_two_exports(lake):
    base = lake / "import" / "telegram"
    for name, unix, text in (("a", TS_2024_A, CHANNEL1_TEXT), ("b", TS_2025, "later-snapshot")):
        d = base / name
        d.mkdir(parents=True)
        (d / "result.json").write_text(json.dumps({
            "chats": {"list": [{"name": "Chan One", "type": "public_channel", "id": CH1, "messages": [_msg(1, text=text, unix=unix)]}]},
        }, ensure_ascii=False), encoding="utf-8")
    report = harvest.run()
    rows = [row for row in report["channel_reports"] if row["id"] == CH1_PEER]
    assert len(rows) == 1
    assert rows[0]["n_messages"] == 2
    assert rows[0]["start"] == datetime(2024, 1, 1, tzinfo=UTC).isoformat()
    assert rows[0]["end"] == datetime(2025, 6, 1, tzinfo=UTC).isoformat()
    assert rows[0]["year_distribution"] == {"2024": 1, "2025": 1}
    assert report["n_messages"] == 2
    assert report["denominator"] == "ordinary_messages_in_this_export"


def test_missing_media_quarantined(account):
    root, directory = account
    doc = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    for chat in doc["chats"]["list"]:
        if chat.get("id") == CH2:
            chat["messages"].append(_msg(2, text="img-missing", unix=TS_2025, photo="chats/chat_002/photos/nope.png"))
    (directory / "result.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    report = harvest.run()
    layout = Layout.from_root(root)
    q = read_quarantine(layout.quarantine_path)
    missing = q.filter(pl.col("reason_code") == "MEDIA_MISSING")
    assert missing.height >= 1
    assert "nope.png" in ";".join(missing["observed_value_ref"].to_list())
    assert_no_secrets(root, report)


def test_type_filter_mutant_leaks_personal(account, monkeypatch):
    from quant_lab.data import sources

    root, _ = account
    report = harvest.run()
    assert_no_secrets(root, report)
    broken = mutant(sources._skip_non_channel, "return chat_type not in CHANNEL_CHAT_TYPES", "return False")
    monkeypatch.setattr(sources, "_skip_non_channel", broken)
    reset_lake(root)
    leaked = harvest.run()
    bronze = pl.read_parquet(Layout.from_root(root).message_version)
    assert PERSONAL_TEXT in bronze["text"].to_list()
    with pytest.raises(AssertionError):
        assert_no_secrets(root, leaked)


def test_canonical_channel_id_mutant_botapi_mismatch(account, monkeypatch):
    from quant_lab.data import sources

    root, _ = account
    (root / "import" / "telegram" / "channels.txt").write_text(f"{CH2_PEER}\n", encoding="utf-8")
    ok = harvest.run()
    assert CH2_PEER in ok["channels"] and ok["n_messages"] == 1
    broken = mutant(sources.canonical_channel_id, "return canonical_peer_id(raw_id, chat_type)", "return raw_id")
    monkeypatch.setattr(sources, "canonical_channel_id", broken)
    reset_lake(root)
    report = harvest.run()
    assert CH2_PEER not in report["channels"]
    assert report["n_messages"] == 0
    assert CH2_PEER in report["whitelist"]["missing"]


def test_left_chats_mutant_drops_left_channel(account, monkeypatch):
    from quant_lab.data import sources

    root, _ = account
    report = harvest.run()
    assert LEFT_PEER in report["channels"]
    assert LEFT_TEXT in pl.read_parquet(Layout.from_root(root).message_version)["text"].to_list()
    broken = mutant(sources._account_sections, 'return (("chats", False), ("left_chats", True))', 'return (("chats", False),)')
    monkeypatch.setattr(sources, "_account_sections", broken)
    reset_lake(root)
    dropped = harvest.run()
    bronze = pl.read_parquet(Layout.from_root(root).message_version)
    assert LEFT_TEXT not in bronze["text"].to_list()
    with pytest.raises(AssertionError):
        assert LEFT_PEER in dropped["channels"] and LEFT_TEXT in bronze["text"].to_list()


def test_whitelist_not_passed_to_normalize_mutant(account):
    root, _ = account
    (root / "import" / "telegram" / "channels.txt").write_text(f"{CH1}\n", encoding="utf-8")
    report = harvest.run()
    assert set(pl.read_parquet(Layout.from_root(root).message_version)["channel_id"].to_list()) == {CH1_PEER}
    assert report["n_messages"] == 2
    broken = mutant(
        harvest.run,
        "normalize.run(source, layout, tdesktop_only=True, allowed_peer_ids=allowed)",
        "normalize.run(source, layout, tdesktop_only=True)",
    )
    reset_lake(root)
    broken()
    bronze = pl.read_parquet(Layout.from_root(root).message_version)
    with pytest.raises(AssertionError):
        assert set(bronze["channel_id"].to_list()) == {CH1_PEER}


def test_media_base_mutant_marks_existing_missing(account, monkeypatch):
    from quant_lab.data import sources

    root, _ = account
    harvest.run()
    layout = Layout.from_root(root)
    assert hashlib.sha256(PNG).hexdigest() in "".join(p.name for p in layout.media_dir.glob("*"))
    assert read_quarantine(layout.quarantine_path).filter(pl.col("reason_code") == "MEDIA_MISSING").height == 0
    broken = mutant(sources._raw_from_tdesktop_item, "media=_media_refs(m, chat_dir)", "media=_media_refs(m, chat_dir.parent)")
    monkeypatch.setattr(sources, "_raw_from_tdesktop_item", broken)
    reset_lake(root)
    harvest.run()
    q = read_quarantine(Layout.from_root(root).quarantine_path)
    with pytest.raises(AssertionError):
        assert hashlib.sha256(PNG).hexdigest() in "".join(p.name for p in Layout.from_root(root).media_dir.glob("*"))
        assert q.filter(pl.col("reason_code") == "MEDIA_MISSING").height == 0


def test_report_body_mutant_killed_by_privacy_assert(account):
    report = harvest.run()
    dumped = json.dumps(report, ensure_ascii=False)
    assert CHANNEL1_TEXT not in dumped
    broken = mutant(harvest.run, 'report["batch_id"] = normalized["batch_id"]', 'report["snippet"] = ordinary[0].text')
    with pytest.raises(AssertionError):
        assert CHANNEL1_TEXT not in json.dumps(broken(), ensure_ascii=False)


@pytest.mark.parametrize("before,after", [
    ('"channel_reports": _merged_channel_reports(in_scope, ordinary, messages)', '"channel_reports": []'),
    ('"missing": missing', '"missing": []'),
])
def test_report_extension_mutant(account, before, after):
    root, _ = account
    (root / "import" / "telegram" / "channels.txt").write_text(f"{CH1}\n{MISSING_ID}\n", encoding="utf-8")
    assert_whitelist_extensions(harvest.run())
    reset_lake(root)
    with pytest.raises(AssertionError):
        assert_whitelist_extensions(mutant(harvest.run, before, after)())


@pytest.mark.parametrize("before,after", [
    ('"n_messages": n', '"n_messages": 0'),
    ('"edit_ratio": _ratio(n, sum(m.edited_raw is not None or m.edited_unixtime is not None or m.edit_time_problem is not None for m in msgs))', '"edit_ratio": 0.0'),
    ('"image_ratio": _ratio(n, sum(any(ref.kind == "photo" for ref in m.media) for m in msgs))', '"image_ratio": 0.0'),
    ('"reply_ratio": _ratio(n, sum(m.reply_to_message_id is not None for m in msgs))', '"reply_ratio": 0.0'),
    ('"year_distribution": dict(sorted(years.items()))', '"year_distribution": {}'),
])
def test_channel_metrics_mutant(account, monkeypatch, before, after):
    assert_ch1_metrics(harvest.run())
    monkeypatch.setattr(harvest, "_channel_metrics", mutant(harvest._channel_metrics, before, after))
    with pytest.raises(AssertionError):
        assert_ch1_metrics(harvest.run())


def test_empty_listing_mutant_drops_empty_channel(account, monkeypatch):
    report = harvest.run()
    assert EMPTY_PEER in {row["id"] for row in report["channel_reports"]}
    monkeypatch.setattr(
        harvest, "_merged_channel_reports",
        mutant(harvest._merged_channel_reports, "if listing.ingested:", "if listing.ingested and listing.n_messages:"),
    )
    with pytest.raises(AssertionError):
        assert EMPTY_PEER in {row["id"] for row in harvest.run()["channel_reports"]}


# ---------------------------------------------------------------- 超级群组（Gauls / Titan / 峰哥 在 Telegram 里是超级群组）

GROUP_SIGNAL_TEXT = "D08_SUPERGROUP_SIGNAL_BTC_LONG_62000"
SUPERGROUP = 111000010
SUPERGROUP_PEER = canonical_peer_id(SUPERGROUP, "private_supergroup")


def _account_with_supergroup():
    return {"chats": {"list": [
        {"name": "Chan One", "type": "public_channel", "id": CH1, "messages": [_msg(1, text=CHANNEL1_TEXT, unix=TS_2024_A)]},
        {"name": "Signal Group", "type": "private_supergroup", "id": SUPERGROUP,
         "messages": [_msg(1, text=GROUP_SIGNAL_TEXT, unix=TS_2024_A)]},
    ]}}


@pytest.mark.parametrize("listed", [str(SUPERGROUP), str(SUPERGROUP_PEER)])
def test_allowlisted_supergroup_is_ingested(tmp_path, listed):
    """白名单点名的超级群组被摄入；裸 id 与 -100 形式都认。"""
    from quant_lab.data.sources import load_channel_whitelist
    (tmp_path / "channels.txt").write_text(f"{listed}\n{CH1}\n", encoding="utf-8")
    allowed = load_channel_whitelist(tmp_path / "channels.txt")
    scan = scan_tdesktop_document(_account_with_supergroup(), tmp_path, raw_uri="result.json", raw_hash="abc", allowed_peer_ids=allowed)
    assert sorted(m.text for m in scan.messages) == sorted([CHANNEL1_TEXT, GROUP_SIGNAL_TEXT])
    assert {m.channel_id for m in scan.messages} == {CH1_PEER, SUPERGROUP_PEER}


def test_supergroup_without_allowlist_is_skipped():
    """没有白名单时，超级群组仍按非频道跳过：群里有其他成员的发言，不能默认摄入。"""
    scan = scan_tdesktop_document(_account_with_supergroup(), pathlib.Path("."), raw_uri="result.json", raw_hash="abc")
    assert [m.text for m in scan.messages] == [CHANNEL1_TEXT]
    assert scan.skipped_by_type == {"private_supergroup": 1}


def test_supergroup_not_on_the_allowlist_is_skipped(tmp_path):
    scan = scan_tdesktop_document(_account_with_supergroup(), tmp_path, raw_uri="result.json", raw_hash="abc",
                                  allowed_peer_ids=frozenset({CH1_PEER}))
    assert [m.text for m in scan.messages] == [CHANNEL1_TEXT]
    assert GROUP_SIGNAL_TEXT not in repr(scan)


def test_mutant_admitting_supergroups_without_allowlist_leaks_group_text(monkeypatch):
    """突变：把「须白名单点名」去掉，超级群组的消息在没有白名单时也进来了。"""
    import quant_lab.data.sources as S
    monkeypatch.setattr(S, "_admitted_supergroup", lambda chat_type, peer, allowed: chat_type in S.SUPERGROUP_CHAT_TYPES)
    monkeypatch.setattr(S, "_scan_whole_account", mutant(
        S._scan_whole_account, "type_key in SUPERGROUP_CHAT_TYPES and allowed_peer_ids is not None", "type_key in SUPERGROUP_CHAT_TYPES"))
    scan = S.scan_tdesktop_document(_account_with_supergroup(), pathlib.Path("."), raw_uri="result.json", raw_hash="abc")
    assert GROUP_SIGNAL_TEXT in [m.text for m in scan.messages]


def test_mutant_without_supergroup_admission_drops_the_target_group(tmp_path, monkeypatch):
    """突变：去掉超级群组准入，白名单点名的目标群被静默跳过——这正是 2026-09-25 实测导出会丢 Gauls/Titan/峰哥 的原因。"""
    import quant_lab.data.sources as S
    monkeypatch.setattr(S, "_admitted_supergroup", lambda chat_type, peer, allowed: False)
    scan = S.scan_tdesktop_document(_account_with_supergroup(), tmp_path, raw_uri="result.json", raw_hash="abc",
                                    allowed_peer_ids=frozenset({CH1_PEER, SUPERGROUP_PEER}))
    assert GROUP_SIGNAL_TEXT not in [m.text for m in scan.messages]
