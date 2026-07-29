"""Thin control-plane wrapper around execution-domain identifier rules."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_EXECUTION_DOMAIN = _REPO / "packages" / "execution-domain"
if str(_EXECUTION_DOMAIN) not in sys.path:
    sys.path.insert(0, str(_EXECUTION_DOMAIN))

from execution_domain.identifiers import (  # noqa: E402
    CanonicalInstrument,
    PositionKey,
    canonical_account_id,
    canonical_instrument_key,
    canonical_position_key,
    instruments_match,
    parse_instrument,
    venue_symbol,
)

__all__ = [
    "CanonicalInstrument",
    "PositionKey",
    "canonical_account_id",
    "canonical_instrument_key",
    "canonical_position_key",
    "instruments_match",
    "parse_instrument",
    "venue_symbol",
]
