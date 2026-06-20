#!/usr/bin/env python3
"""Build a MECHANICAL corpus manifest from a telegram-watcher fixture.

No semantic parsing, no importer, no auto-approval. Pure mechanical:
  - canonical provenance mapping
  - sha256 of media + text
  - original ordering preserved
  - objective counts only
Semantic classification (new_signal/update/analysis/ambiguous) and gold labels
are intentionally LEFT OUT — those require human/Hermes review (PLAN C.4/C.5).
Usage: build_corpus_manifest.py <messages.json> <media_dir> <out.json>
"""
from __future__ import annotations
import hashlib, json, sys
from pathlib import Path

def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()

def main(argv):
    msgs_path, media_dir, out_path = Path(argv[1]), Path(argv[2]), Path(argv[3])
    msgs = json.load(msgs_path.open(encoding="utf-8"))
    out_msgs, with_image, with_text, incident_candidates = [], 0, 0, []
    for seq, m in enumerate(msgs):
        text = m.get("text") or ""
        media = []
        if m.get("media"):
            mm = m["media"]
            f = media_dir / mm["filename"]
            media.append({
                "sha256": sha256_file(f) if f.exists() else None,
                "mime": mm.get("mimeType"), "object_key": mm.get("path"),
                "size": mm.get("size"), "exists": f.exists(),
            })
            with_image += 1
        if text.strip():
            with_text += 1
        low = text.lower()
        for tok in ("hype", "inj"):
            if tok in low:
                incident_candidates.append({"corpus_seq": seq, "id": m.get("id"), "token": tok.upper()})
        out_msgs.append({
            "corpus_seq": seq,
            "source": "telegram",
            "channel_id": str(m.get("chatId")),
            "chat_title": m.get("chatTitle"),
            "source_message_id": str(m.get("id")),
            "source_version": 0,                 # fixture carries no edit version
            "source_received_at": m.get("date"),
            "reply_to_message_id": None,         # not present in fixture
            "edit_of_message_id": None,
            "text_present": bool(text.strip()),
            "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "media": media,
        })
    manifest = {
        "status": "MECHANICAL_PARTIAL — semantic classification & gold labels pending human/Hermes",
        "corpus_id": msgs_path.parent.name,
        "objective_counts": {
            "total": len(out_msgs), "with_image": with_image,
            "with_text": with_text, "empty_text": len(out_msgs) - with_text,
        },
        "semantic_category_counts": "PENDING_HUMAN",   # never machine-guessed
        "incident_candidates": incident_candidates,     # string-match only, human confirms
        "messages": out_msgs,
    }
    json.dump(manifest, out_path.open("w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(json.dumps({k: manifest[k] for k in ("status","objective_counts","incident_candidates")},
                     ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
