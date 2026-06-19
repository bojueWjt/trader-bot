from __future__ import annotations

import re


PAIR_IGNORE_WORDS = {
    "CMP",
    "DCA",
    "ENTRY",
    "LONG",
    "SHORT",
    "SL",
    "TP",
    "USDT",
}


def map_pair_to_freqtrade(pair_raw: str) -> str:
    cleaned = pair_raw.strip().upper()
    cleaned = cleaned.replace("$", "")
    cleaned = cleaned.replace("#", "")
    cleaned = cleaned.replace("-", "/")
    if cleaned.endswith(":USDT"):
        return cleaned
    if "/" in cleaned:
        base, quote = cleaned.split("/", 1)
        if quote == "USDT":
            return f"{base}/USDT:USDT"
        return f"{base}/{quote}:USDT"
    if cleaned.endswith("USDT"):
        base = cleaned[:-4]
        return f"{base}/USDT:USDT"
    return f"{cleaned}/USDT:USDT"


def extract_pair_raw(raw_text: str) -> str:
    patterns = [
        r"[$#]\s*([A-Z0-9]{2,15})(?:USDT)?\b",
        r"\b([A-Z0-9]{2,15})/USDT\b",
        r"\b([A-Z0-9]{2,15})USDT\b",
        r"\b([A-Z0-9]{2,15})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw_text, re.IGNORECASE)
        if not match:
            continue
        token = match.group(1).upper()
        if token in PAIR_IGNORE_WORDS:
            continue
        if token.endswith("USDT"):
            return token
        return f"{token}USDT"
    return "UNKNOWNUSDT"
