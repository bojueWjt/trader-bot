# Real-corpus replay — Window A pipeline

Local replay of the **real 80-message / 50-image** trading-signal corpus (provided in the
Window C `hk-replay` package, `telegram_watcher_80msg_50img`) through the Window A pipeline
on a throwaway PostgreSQL 16 cluster. Safe and local — no live server.

## How to run

```bash
# temp pg@16 (UTC), migrate 0001..0003, then:
DATABASE_URL=postgresql://... \
  python scripts/replay_real_corpus.py --corpus <.../telegram_watcher_80msg_50img>
```

## Result (real data)

```json
{
  "messages": 80,
  "ingest_pass1": {"inserted": 75, "duplicates": 5, "media": 50, "missing_media": 0},
  "raw_messages_after_pass1": 75, "media_assets_after_pass1": 48,
  "ingest_pass2": {"inserted": 0, "duplicates": 80}, "raw_messages_after_pass2": 75,
  "idempotent": true,
  "worker_outcomes": {"succeeded": 75}, "hermes_decisions": 75,
  "gateway_outcomes": {"needs_review": 75}, "risk_decisions": 75, "trade_intents": 0
}
```

## What this proves (gate #8 — data side)

- **Real text + image ingestion**: 80 real messages → 75 canonical `raw_messages` + 48 `media_assets` with **real sha256** computed from the actual jpg bytes.
- **Idempotency**: re-ingesting the whole corpus inserts 0 new rows (content-hash + unique `(source,channel,source_message_id,source_version)`).
- **Real media handling**: the Hermes worker loaded the **real image bytes** (base64) for every media message before calling the model.
- **Fail-closed end to end**: with the model call mocked (no local endpoint) returning `needs_review`, the pipeline produced 75 decisions → 75 `needs_review` risk decisions → **0 trade intents**. No risk is ever fabricated without the real model.

## Findings about the corpus

- **5 messages share a duplicate `(chat_id, message_id)`** key; under a single `source_version=v1` they dedupe to 75. If these are edits, a faithful edit-replay should assign incrementing `source_version` so each edit becomes a new immutable version (matches A-03 edit handling). Recorded as a refinement for the real replay.
- 2 of the 50 media were attached to the deduped messages, so 48 distinct `media_assets` landed.

## Still BLOCKED (gate #8 — model side)

The **real multimodal Hermes call** (real classifications/approvals over these images) requires
`HERMES_API_URL/HERMES_API_KEY/HERMES_MODEL`. `services/hermes-worker/smoke_replay.py` runs that
real path and reports `BLOCKED` until configured. This replay validates everything *around* the
model call on real data; the model call itself is the remaining Window C gate.
