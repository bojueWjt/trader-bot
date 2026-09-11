#!/usr/bin/env python3
"""生成录制 LLM 抽取夹具（合成响应，usage.synthetic=true）。运行：.venv-g1/bin/python tests/data/fixtures/gen_llm_recorded.py

key = record_key(system, user, schema_name)，user 由 build_extract_prompt 生成；prompt 模板变更需重跑本脚本。
含一条故意伪造 span 的响应（B 纯图消息声称数字）用于验证证据校验拒收。
"""
from __future__ import annotations

import json
import pathlib

from quant_lab.data.llm import SCHEMA_NAME_EXTRACT, build_extract_prompt, record_key
from quant_lab.data.sources import read_all

FIX = pathlib.Path(__file__).parent / "tdesktop_sample"
OUT = pathlib.Path(__file__).parent / "llm_recorded" / "extract_v1.json"
ANCH = json.loads((FIX / "ANCHORS.json").read_text())
PEER, A = ANCH["peers"], ANCH["anchors"]


def span_of(text: str, token: str, field: str, nth: int = 0) -> dict:
    i = -1
    for _ in range(nth + 1):
        i = text.index(token, i + 1)
    return {"field": field, "start": i, "end": i + len(token)}


def main() -> None:
    msgs = {(m.channel_id, m.message_id): m for m in read_all(FIX)}
    items: dict[str, dict] = {}

    def rec(peer_key: str, mid: int, response: dict | None, *, abstain: dict | None = None, usage: dict | None = None):
        m = msgs[(PEER[peer_key], mid)]
        system, user = build_extract_prompt(m.text, channel_name=m.channel_name, message_date=m.message_date.isoformat() if m.message_date else None)
        key = record_key(system, user, SCHEMA_NAME_EXTRACT)
        entry = {"peer": peer_key, "message_id": mid, "usage": usage or {"prompt_tokens": 900, "completion_tokens": 120, "synthetic": True}}
        if abstain:
            entry["abstain"] = abstain
        else:
            entry["response"] = response
        items[key] = entry
        return m.text

    # A#103 舒琴 ETH 多（完整）
    t = rec("A", A["A_eth_1"], None)  # placeholder to fetch text
    resp = {
        "kind": "entry_proposal", "symbol_raw": "ETH", "side": "long",
        "entry": {"lo": 3295, "hi": 3315, "kind": "zone"}, "entries": [], "stop": 3268,
        "tps": [{"level": 3360}, {"level": 3430}, {"level": 3530}],
        "spans": [span_of(t, "3295", "entry.lo"), span_of(t, "3315", "entry.hi"), span_of(t, "3268", "stop"),
                  span_of(t, "3360", "tps"), span_of(t, "3430", "tps"), span_of(t, "3530", "tps")],
        "reason_codes": [],
    }
    rec("A", A["A_eth_1"], resp)
    # A#120 反例1 编辑版（LLM 也只看到最终版）
    t = rec("A", A["CE1_edited_sl"], None)
    rec("A", A["CE1_edited_sl"], {
        "kind": "entry_proposal", "symbol_raw": "BTC", "side": "long", "entry": {"lo": 62000, "hi": 62500, "kind": "zone"}, "entries": [],
        "stop": 60800, "tps": [{"level": 63500}, {"level": 64500}, {"level": 66000}],
        "spans": [span_of(t, "62000", "entry.lo"), span_of(t, "62500", "entry.hi"), span_of(t, "60800", "stop"),
                  span_of(t, "63500", "tps"), span_of(t, "64500", "tps"), span_of(t, "66000", "tps")], "reason_codes": []})
    # A#160 反例5 提前管理
    t = rec("A", A["CE5_orphan_mgmt"], None)
    rec("A", A["CE5_orphan_mgmt"], {"kind": "stop_move", "symbol_raw": "BTC", "side": "short", "entry": None, "entries": [], "stop": 65000, "tps": [],
                                    "spans": [span_of(t, "65000", "stop")], "reason_codes": []})
    # A#131 反例2 "到达 3000，止盈一半" → LLM 说 reduce（与 parser 一致）
    t = rec("A", A["CE2_unfilled_tp"] + 2, None)
    rec("A", A["CE2_unfilled_tp"] + 2, {"kind": "reduce", "symbol_raw": "ETH", "side": None, "entry": None, "entries": [], "stop": None, "tps": [{"level": 3000}],
                                        "spans": [span_of(t, "3000", "tps")], "reason_codes": []})
    # B 纯图：LLM 伪造数字（无 span 证据）→ 必须被拒收
    rec("B", A["B_pure_image"], {"kind": "entry_proposal", "symbol_raw": "BTC", "side": "long", "entry": {"lo": 64000, "hi": 64500, "kind": "zone"},
                                 "entries": [], "stop": 63000, "tps": [], "spans": [], "reason_codes": []})
    # B ZRO 相册说明：span 错位（声称 0.7102 但 span 指向 0.688）→ 拒收
    t = rec("B", A["B_album_inferred"], None)
    rec("B", A["B_album_inferred"], {"kind": "entry_proposal", "symbol_raw": "ZRO", "side": "long", "entry": {"lo": 0.688, "hi": 0.7102, "kind": "ladder"},
                                     "entries": [0.7102, 0.688], "stop": 0.6666, "tps": [],
                                     "spans": [span_of(t, "0.688", "entries"), span_of(t, "0.688", "entries"), span_of(t, "0.6666", "stop")], "reason_codes": []})
    # C 闲聊：LLM 拒答出口
    rec("C", A["C_dup_first"], None, abstain={"reason_code": "NOT_SIGNAL", "note": "chatter"})
    # A#140 反例3 超时（LLM 与 parser 一致，含期限）
    t = rec("A", A["CE3_timeout"], None)
    rec("A", A["CE3_timeout"], {"kind": "entry_proposal", "symbol_raw": "SOL", "side": "short", "entry": {"lo": 150, "hi": 152, "kind": "zone"}, "entries": [], "stop": 156,
                                "tps": [{"level": 145}, {"level": 140}, {"level": 132}],
                                "spans": [span_of(t, "150", "entry.lo"), span_of(t, "152", "entry.hi"), span_of(t, "156", "stop"), span_of(t, "145", "tps"), span_of(t, "140", "tps"), span_of(t, "132", "tps")],
                                "reason_codes": []})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"version": "fixture-v1", "model": "synthetic-recorded", "prompt_version": "extract-v1", "items": items}, ensure_ascii=False, indent=1))
    print("recorded", len(items), "->", OUT)


if __name__ == "__main__":
    main()
