"""Diagnostic: send A's exact Hermes request for one corpus message, print raw response."""
import base64
import json
import os
import sys
import urllib.request

sys.path[:0] = ["/srv/trader-v3/services/hermes-worker"]
from hermes_client import HermesImage, HermesRequest  # noqa: E402
from prompt import MODEL_TEMPERATURE, build_messages  # noqa: E402

img = base64.b64encode(open("/srv/trader-v3/media/1771468201173-3947.jpg", "rb").read()).decode()
req = HermesRequest(
    raw_message_id="diag-1",
    text="#2Z 和 #AAVE 交易更新\n\n我们的两个入场点几乎完美触发✅ 交易已经盈利",
    images=[HermesImage(sha256="x", mime="image/jpeg", data_base64=img)],
    system_snapshot={"data_source": "postgres_projection", "stale": False, "balances": {}, "positions": []},
)
body = {
    "model": os.environ["HERMES_MODEL"],
    "temperature": MODEL_TEMPERATURE,
    "response_format": {"type": "json_object"},
    "messages": build_messages(req),
}
url = os.environ["HERMES_API_URL"].rstrip("/") + "/chat/completions"
r = urllib.request.urlopen(
    urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["HERMES_API_KEY"]},
    ),
    timeout=90,
)
p = json.loads(r.read())
ch = p["choices"][0]
msg = ch["message"]
print("finish_reason:", ch.get("finish_reason"))
print("message keys:", list(msg.keys()))
print("content repr:", repr(msg.get("content"))[:400])
rc = msg.get("reasoning_content") or msg.get("reasoning")
print("reasoning len:", len(rc) if rc else 0)
print("usage:", p.get("usage"))
