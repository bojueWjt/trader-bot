"""Pinned Hermes trader prompt (hermes-trader-v1).

The prompt is the only place semantics are interpreted; it instructs the model to
emit strict HermesDecisionV1 JSON. Versioned so decisions are reproducible/auditable.
"""

from __future__ import annotations

from typing import Any

PROMPT_VERSION = "hermes-trader-v1"
MODEL_TEMPERATURE = 0

SYSTEM_PROMPT = """\
You are Hermes, the single semantic processor for a crypto trading desk. You read the
raw Telegram message text, ALL attached images, any quoted/replied messages, recent
context, and a real system snapshot, then output exactly one JSON object that conforms
to the HermesDecisionV1 schema. Rules:
- Output ONLY the JSON object, no prose.
- schema_version is "1.0"; model.provider is "hermes"; temperature is 0.
- If the message is ambiguous, a required image is missing/unreadable, or a target
  position cannot be uniquely identified, set classification.action to "needs_review"
  and classification.ambiguous to true with ambiguity_reasons.
- Update messages (position_update/close_update) must NOT produce "open_position".
- close_position/partial_close/move_stop_* must reference a real target_position_id;
  otherwise use "needs_review".
- Never invent prices/levels not supported by the message or images.
- Do not echo secrets or credentials.
"""


def _image_content(image: Any) -> dict[str, Any]:
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{image.mime};base64,{image.data_base64}"},
    }


def build_messages(request: Any) -> list[dict[str, Any]]:
    """Assemble OpenAI-compatible multimodal chat messages from a HermesRequest."""
    user_content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "raw_message_id: " + str(request.raw_message_id) + "\n"
                "message_text:\n" + (request.text or "")
            ),
        }
    ]
    for image in request.images:
        user_content.append(_image_content(image))

    context_blob = {
        "referenced_messages": request.referenced_messages,
        "recent_context": request.recent_context,
        "system_snapshot": request.system_snapshot,
    }
    user_content.append(
        {"type": "text", "text": "context_json:\n" + _json(context_blob)}
    )

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
