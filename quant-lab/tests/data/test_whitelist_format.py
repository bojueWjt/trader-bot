"""channels.txt 的格式与报错归属（2026-09-25 真实运行：行尾注释让整批被报成「导出无效」）。"""
from __future__ import annotations

import pathlib

import pytest

from quant_lab.data import harvest
from quant_lab.data.sources import ChannelWhitelistInvalid, canonical_peer_id, load_channel_whitelist


def test_trailing_comments_are_allowed(tmp_path):
    p = tmp_path / "channels.txt"
    p.write_text("# 标题\n-1002136478186  # 舒琴\n2198013097 # Titan\n\n", encoding="utf-8")
    assert load_channel_whitelist(p) == frozenset({-1002136478186, canonical_peer_id(2198013097, "channel")})


def test_bad_line_is_named_by_line_number_without_its_content(tmp_path):
    p = tmp_path / "channels.txt"
    p.write_text("-1002136478186\nnot-an-id-SECRET\n", encoding="utf-8")
    with pytest.raises(ChannelWhitelistInvalid) as ei:
        load_channel_whitelist(p)
    assert ei.value.lineno == 2 and "SECRET" not in str(ei.value)


def test_harvest_reports_the_whitelist_not_the_export(tmp_path, monkeypatch):
    root = tmp_path / "data"
    imp = root / "import" / "telegram"
    src = pathlib.Path(__file__).parent / "fixtures" / "tdesktop_sample" / "AlphaSignals"
    (imp / "AlphaSignals").mkdir(parents=True)
    for f in src.rglob("*"):
        if f.is_file():
            dest = imp / "AlphaSignals" / f.relative_to(src)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(f.read_bytes())
    (imp / "channels.txt").write_text("oops\n", encoding="utf-8")
    monkeypatch.setenv("QUANT_LAB_DATA_ROOT", str(root))
    with pytest.raises(harvest.HarvestRefusal) as ei:
        harvest.run()
    assert str(ei.value) == "CHANNEL_WHITELIST_INVALID:line1"


def test_appledouble_companions_in_pull_dir_are_ignored(tmp_path):
    """exFAT 上 macOS 会写 ._name.jsonl 伴生文件（二进制元数据），导入不得把它当数据读。"""
    from quant_lab.data.sources import ingest_pull_dir
    pull = tmp_path / "pull"
    pull.mkdir()
    (pull / "._-1002136478186.jsonl").write_bytes(b"\x00\x05\x16\x07AppleDouble-not-json")
    msgs, listings = ingest_pull_dir(pull, allowed_peer_ids=None, root=tmp_path)
    assert msgs == [] and listings == []
    assert harvest._pull_jsonl_files(tmp_path) == []


def test_export_reactions_and_inviter_are_not_schema_drift(tmp_path):
    """Newer TDesktop exports add export-time reaction counts; they must not quarantine the message."""
    from quant_lab.data.sources import _raw_from_tdesktop_item
    base = {"id": 1, "type": "message", "date": "2025-01-01T00:00:00", "date_unixtime": "1735689600", "text": "仿写 BTC 做多"}
    kw = dict(peer=-100, name="虚构", raw_uri="x", raw_hash="h", snapshot_at=None, chat_dir=tmp_path)
    ok = _raw_from_tdesktop_item(dict(base, reactions=[{"type": "emoji", "count": 3, "emoji": "👍"}], inviter="x"), 0, **kw)
    assert ok.unknown_keys == []
    drift = _raw_from_tdesktop_item(dict(base, brand_new_field=1), 0, **kw)
    assert drift.unknown_keys == ["brand_new_field"]
