from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import re
from typing import Any

from app.services.pair_mapper import extract_pair_raw, map_pair_to_freqtrade


PARSER_VERSION = "signal-parser-v1"
SIGNAL_MAX_AGE_MINUTES = 240


class SignalStatus(str, Enum):
    RAW = "raw"
    PARSED = "parsed"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    RESERVED = "reserved"
    SENT_TO_FREQTRADE = "sent_to_freqtrade"
    ENTERED = "entered"
    PARTIALLY_EXITED = "partially_exited"
    EXITED = "exited"
    REJECTED = "rejected"
    EXPIRED = "expired"
    FAILED = "failed"
    IGNORED = "ignored"
    BLOCKED_BY_RISK = "blocked_by_risk"


@dataclass(frozen=True)
class MediaAsset:
    type: str
    path: str
    mime_type: str = ""


@dataclass
class EntryPlan:
    mode: str = "cmp"
    primary_price: float | None = None
    dca_prices: list[float] = field(default_factory=list)
    valid_from: datetime | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True)
class TakeProfit:
    price: float
    close_pct: float = 100


@dataclass(frozen=True)
class LeveragePlan:
    min: int = 3
    max: int = 3
    selected: int = 3


@dataclass
class ParsedSignal:
    signal_id: str
    source: str
    source_channel_id: str
    source_channel_name: str
    source_message_id: str
    received_at: datetime
    raw_text: str
    media: list[MediaAsset]
    pair_raw: str
    pair_freqtrade: str
    side: str
    entry: EntryPlan
    stop_loss: float | None
    take_profits: list[TakeProfit]
    status: SignalStatus
    parser_version: str = PARSER_VERSION
    review_reason_codes: list[str] = field(default_factory=list)
    take_profit_parse_status: str = "parsed"
    leverage: LeveragePlan = field(default_factory=LeveragePlan)


def parse_signal(
    raw_message: str | dict[str, Any],
    pair_whitelist: set[str] | None = None,
    current_prices: dict[str, float] | None = None,
) -> ParsedSignal:
    parser = SignalParser(pair_whitelist=pair_whitelist)
    return parser.parse(raw_message, current_prices=current_prices)


class SignalParser:
    def __init__(self, pair_whitelist: set[str] | None = None) -> None:
        if pair_whitelist:
            self.pair_whitelist = pair_whitelist
        else:
            self.pair_whitelist = set()

    def parse(
        self,
        raw_message: str | dict[str, Any],
        current_prices: dict[str, float] | None = None,
    ) -> ParsedSignal:
        message = self._normalize_message(raw_message)
        raw_text = message["raw_text"]
        pair_raw = extract_pair_raw(raw_text)
        pair_freqtrade = map_pair_to_freqtrade(pair_raw)
        received_at = parse_utc_datetime(message["received_at"])
        take_profits = self._extract_take_profits(raw_text)
        signal = ParsedSignal(
            signal_id=message["signal_id"],
            source=message["source"],
            source_channel_id=message["source_channel_id"],
            source_channel_name=message["source_channel_name"],
            source_message_id=message["source_message_id"],
            received_at=received_at,
            raw_text=raw_text,
            media=self._normalize_media(message["media"]),
            pair_raw=pair_raw,
            pair_freqtrade=pair_freqtrade,
            side=self._extract_side(raw_text),
            entry=self._extract_entry(raw_text, received_at),
            stop_loss=self._extract_stop_loss(raw_text),
            take_profits=take_profits,
            take_profit_parse_status=self._take_profit_parse_status(raw_text, take_profits),
            leverage=self._extract_leverage(raw_text),
            status=SignalStatus.PARSED,
        )
        self._apply_review_reasons(signal, current_prices)
        return signal

    def _normalize_message(self, raw_message: str | dict[str, Any]) -> dict[str, Any]:
        if isinstance(raw_message, str):
            return {
                "signal_id": "manual:0",
                "source": "manual",
                "source_channel_id": "manual",
                "source_channel_name": "",
                "source_message_id": "0",
                "received_at": False,
                "raw_text": raw_message,
                "media": [],
            }

        source_channel_id = str(raw_message.get("source_channel_id", "manual"))
        source_message_id = str(raw_message.get("source_message_id", "0"))
        signal_id = raw_message.get("signal_id")
        if not signal_id:
            signal_id = f"{source_channel_id}:{source_message_id}"

        return {
            "signal_id": str(signal_id),
            "source": str(raw_message.get("source", "manual")),
            "source_channel_id": source_channel_id,
            "source_channel_name": str(raw_message.get("source_channel_name", "")),
            "source_message_id": source_message_id,
            "received_at": raw_message.get("received_at", False),
            "raw_text": str(raw_message.get("raw_text", "")),
            "media": list(raw_message.get("media", [])),
        }

    def _normalize_media(self, media_items: list[dict[str, Any]]) -> list[MediaAsset]:
        media: list[MediaAsset] = []
        for item in media_items:
            media.append(
                MediaAsset(
                    type=str(item.get("type", "")),
                    path=str(item.get("path", "")),
                    mime_type=str(item.get("mime_type", item.get("mimeType", ""))),
                )
            )
        return media

    def _extract_side(self, raw_text: str) -> str:
        if re.search(r"\bLONG\b|做多|多单", raw_text, re.IGNORECASE):
            return "long"
        if re.search(r"\bSHORT\b|空单|空头|做空", raw_text, re.IGNORECASE):
            return "short"
        return "long"

    def _extract_entry(self, raw_text: str, received_at: datetime) -> EntryPlan:
        dca_prices = self._extract_prices_after_label(raw_text, r"DCA|补仓|定投(?:入场|开单)?(?:点)?")
        primary_price = self._extract_primary_price(raw_text)
        mode = "cmp"
        has_cmp = self._has_cmp_text(raw_text)
        if dca_prices and has_cmp:
            mode = "cmp_plus_dca"
        elif dca_prices:
            mode = "limit_plus_dca"
        elif primary_price:
            mode = "limit"
        return EntryPlan(
            mode=mode,
            primary_price=primary_price,
            dca_prices=dca_prices,
            valid_from=received_at,
            expires_at=received_at + timedelta(minutes=SIGNAL_MAX_AGE_MINUTES),
        )

    def _extract_primary_price(self, raw_text: str) -> float | None:
        patterns = [
            r"首次\s*([0-9]+(?:\.[0-9]+)?)",
            r"\bEntry\s*[:=]\s*(?!CMP\b|现价)([0-9]+(?:\.[0-9]+)?)",
        ]
        for pattern in patterns:
            match = re.search(pattern, raw_text, re.IGNORECASE)
            if match:
                return float(match.group(1))
        for line in raw_text.splitlines():
            if re.search(r"DCA|补仓|定投", line, re.IGNORECASE):
                continue
            if not re.search(r"首次(?:入场|开单)?(?:点)?|Entry|入场点|开单价", line, re.IGNORECASE):
                continue
            if self._has_cmp_text(line):
                continue
            prices = self._prices_from_line(line)
            if prices:
                return prices[0]
        return None

    def _extract_stop_loss(self, raw_text: str) -> float | None:
        prices = self._extract_prices_after_label(raw_text, r"SL(?:止损)?|Stop\s*Loss|止损(?:位)?")
        if not prices:
            return None
        return prices[0]

    def _extract_take_profits(self, raw_text: str) -> list[TakeProfit]:
        prices = self._extract_prices_after_label(
            raw_text,
            r"TP\d*(?:目标|目標)?|Take\s*Profit|止盈(?:目标|目標|点位)?|目标价|目標价",
        )
        return [TakeProfit(price=price) for price in prices]

    def _extract_prices_after_label(self, raw_text: str, label_pattern: str) -> list[float]:
        prices: list[float] = []
        for line in raw_text.splitlines():
            match = re.search(label_pattern, line, re.IGNORECASE)
            if not match:
                continue
            tail = line[match.end() :]
            prices.extend(self._prices_from_line(tail))
        return prices

    def _prices_from_line(self, line: str) -> list[float]:
        prices: list[float] = []
        for match in re.finditer(r"[$￥]?\s*([0-9]+(?:\.[0-9]+)?)(?![0-9.]|\s*%)", line):
            prices.append(float(match.group(1)))
        return prices

    def _extract_leverage(self, raw_text: str) -> LeveragePlan:
        match = re.search(r"(\d+)\s*(?:倍|x|X)\s*(?:-|~|至|到)\s*(\d+)\s*(?:倍|x|X)", raw_text)
        if not match:
            return LeveragePlan()
        min_leverage = int(match.group(1))
        max_leverage = int(match.group(2))
        selected = 3
        if selected > max_leverage:
            selected = max_leverage
        if selected < min_leverage:
            selected = min_leverage
        return LeveragePlan(min=min_leverage, max=max_leverage, selected=selected)

    def _take_profit_parse_status(self, raw_text: str, take_profits: list[TakeProfit]) -> str:
        if take_profits:
            return "parsed"
        if re.search(r"\bTP\b|止盈", raw_text, re.IGNORECASE):
            return "missing_explicit_price"
        return "missing"

    def _apply_review_reasons(
        self,
        signal: ParsedSignal,
        current_prices: dict[str, float] | None,
    ) -> None:
        reason_codes: list[str] = []
        if self.pair_whitelist and signal.pair_freqtrade not in self.pair_whitelist:
            reason_codes.append("pair_not_whitelisted")
        if signal.stop_loss is None:
            reason_codes.append("stop_loss_missing")
        if signal.take_profit_parse_status == "missing_explicit_price":
            reason_codes.append("tp_price_missing_from_text")
        reason_codes.extend(self._geometry_review_reasons(signal, current_prices))
        signal.review_reason_codes = sorted(set(reason_codes))
        if signal.review_reason_codes:
            signal.status = SignalStatus.NEEDS_REVIEW

    def _geometry_review_reasons(
        self,
        signal: ParsedSignal,
        current_prices: dict[str, float] | None,
    ) -> list[str]:
        if not current_prices:
            return []
        entry_price = signal.entry.primary_price
        if entry_price is None:
            entry_price = current_prices.get(signal.pair_freqtrade)
        if entry_price is None:
            return []
        if signal.stop_loss is None:
            return ["stop_loss_missing"]
        take_profit_prices = [take_profit.price for take_profit in signal.take_profits]
        if not take_profit_prices:
            return []
        if signal.side == "long":
            if signal.stop_loss < entry_price and all(price > entry_price for price in take_profit_prices):
                return []
            return ["long_price_geometry_invalid"]
        if signal.side == "short":
            if signal.stop_loss > entry_price and all(price < entry_price for price in take_profit_prices):
                return []
            return ["short_price_geometry_invalid"]
        return ["side_invalid"]

    def _has_cmp_text(self, text: str) -> bool:
        return bool(re.search(r"(?<![A-Z0-9])CMP(?![A-Z0-9])|现价|市場价|市场价", text, re.IGNORECASE))


def parse_utc_datetime(value: str | datetime | bool) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo:
            return value
        return value.replace(tzinfo=timezone.utc)
    if not value:
        return datetime.now(tz=timezone.utc)
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo:
        return parsed
    return parsed.replace(tzinfo=timezone.utc)
