#!/usr/bin/env python3
"""录制 OCR 夹具（合成）：按夹具图片 sha256 回放 {text, numbers[{value,bbox}]}。运行：.venv-g1/bin/python tests/data/fixtures/gen_ocr_recorded.py
- B 纯图（B_pure_image）：可读计划 "BTC 做多 入场 64000-64500 止损 63000"（数字带 bbox）
- B 图文信号（B_edited_photo_signal，Titan BTC SHORT 64570/64900/66200）：图上止损 66200 与文字一致 → 无冲突
- B 第二个相册首图（B_album_explicit，FIL）：图上止损 5.5（文字 5.998）→ TEXT_IMAGE_CONFLICT
- 其余图片：未录制 → unreadable
"""
from __future__ import annotations

import hashlib
import json
import pathlib

FIX = pathlib.Path(__file__).parent / "tdesktop_sample"
OUT = pathlib.Path(__file__).parent / "llm_recorded" / "ocr_v1.json"
ANCH = json.loads((FIX / "ANCHORS.json").read_text())
A = ANCH["anchors"]


def photo_hash(chat_dir: str, mid: int) -> str:
    doc = json.loads((FIX / chat_dir / "result.json").read_text())
    m = next(x for x in doc["messages"] if x["id"] == mid)
    return hashlib.sha256((FIX / chat_dir / m["photo"]).read_bytes()).hexdigest()


def main() -> None:
    from draw_numbers import draw, SIZE
    cases = [
        (A["B_pure_image"], ["64000", "64500", "63000"], "BTC 做多 入场 64000-64500 止损 63000"),
        (A["B_edited_photo_signal"], ["64570", "64900", "66200"], "BTC 做空 首次入场价 64570 第二次入场价 64900 止损 66200"),
        (A["B_album_explicit"], ["6.328", "6.156", "5.5"], "FIL 做多 首次入场价 6.328 第二次入场价 6.156 止损 5.5"),
    ]
    items = {}
    doc = json.loads((FIX / "BetaTrades" / "result.json").read_text())
    labels_by_case = [
        ["BTC LONG ENTRY LO", "ENTRY HI", "STOP"],
        ["BTC SHORT ENTRY 1", "ENTRY 2", "STOP"],
        ["FIL LONG ENTRY 1", "ENTRY 2", "STOP"],
    ]
    for (mid, values, text), labels in zip(cases, labels_by_case):
        message = next(m for m in doc["messages"] if m["id"] == mid)
        png, numbers = draw(values, labels=labels)
        (FIX / "BetaTrades" / message["photo"]).write_bytes(png)
        items[hashlib.sha256(png).hexdigest()] = {"size": SIZE, "text": text, "numbers": numbers}
    # Paired degraded pixels: the unreadable recording refers to an actual degraded image.
    spec = json.loads((FIX / "SPEC.json").read_text())["channels"]["B"]
    blurred_id = next(int(mid) for mid, category in spec.items() if category == "image" and int(mid) != A["B_pure_image"])
    blurred_message = next(m for m in doc["messages"] if m["id"] == blurred_id)
    blurred_png, _ = draw(["64000", "64500", "63000"], unreadable=True)
    (FIX / "BetaTrades" / blurred_message["photo"]).write_bytes(blurred_png)
    items[hashlib.sha256(blurred_png).hexdigest()] = {"size": SIZE, "unreadable": True, "numbers": [], "paired_with": photo_hash("BetaTrades", A["B_pure_image"])}
    for photo in FIX.rglob("*.png"):
        h = hashlib.sha256(photo.read_bytes()).hexdigest()
        if h not in items:
            items[h] = {"size": SIZE, "unreadable": True, "numbers": []}
    OUT.write_text(json.dumps({"version": "ocr-fixture-v3", "items": items}, ensure_ascii=False, indent=1))
    print("ocr recorded", len(items), "->", OUT)


if __name__ == "__main__":
    main()
