from __future__ import annotations

from datetime import timedelta
import re
from typing import Any

from freqtrade.signal_strategy.domain import (
    DirectiveKind,
    EntryPlan,
    LeveragePlan,
    MediaAsset,
    MessageType,
    PositionDirective,
    RiskPolicyResult,
    SignalStatus,
    TakeProfit,
    TradingSignal,
    parse_utc_datetime,
)
from freqtrade.signal_strategy.risk import RiskContext, RiskGovernor, RiskPolicy


PARSER_VERSION = "signal-parser-v1"
PAIR_IGNORE_WORDS = {
    "CMP",
    "DCA",
    "ENTRY",
    "H1",
    "H4",
    "LONG",
    "M5",
    "M15",
    "SHORT",
    "SL",
    "TP",
    "USDT",
}
PAIR_ALIASES = (
    ("比特币", "BTCUSDT"),
    ("以太坊", "ETHUSDT"),
)
UPDATE_HEADLINE_PATTERN = r"交易更新|最新动态|平仓|UPDATE\b|CLOSE\b|CLOSED\b"
BUY_PLAN_PATTERN = r"买入(?:交易计划|策略|计划|信号)|\bBUY\b|\bLONG\b"
ENTRY_LABEL_PATTERN = r"入场点|首次建仓|建仓区间|建仓|开仓|Entry"
RISK_LABEL_PATTERN = r"止损位|止损|SL\b|Stop\s*Loss"
PROFIT_LABEL_PATTERN = r"目标价|止盈目标|止盈|TP\b|Take\s*Profit"

PARTIAL_DIRECTIVE_PATTERNS = (
    r"锁定\s*\d+(?:\.\d+)?\s*%",
    r"锁定\s*(?:\d+(?:\.\d+)?\s*%)?\s*(?:的)?\s*(?:利润|收益)",
    r"锁定部分",
    r"锁利",
    r"减仓",
    r"lock\s*(?:\d+(?:\.\d+)?\s*%)?\s*profit",
    r"partial\s*close",
)
PARTIAL_DEFAULT_PATTERNS = (
    r"部分",
    r"partial",
    r"锁利",
    r"减仓",
)
MOVE_SL_TO_ENTRY_PATTERNS = (
    r"无风险",
    r"保本",
    r"risk[.\s-]*free",
    r"move\s*sl\s*to\s*entry",
    r"break[.\s-]*even",
)
MOVE_SL_PRICE_PATTERNS = (
    r"止损上移至\s*([0-9]+(?:\.[0-9]+)?)",
    r"stop[.\s-]*loss.*?\bto\s+([0-9]+(?:\.[0-9]+)?)",
    r"\bsl\b.*?\bto\s+([0-9]+(?:\.[0-9]+)?)",
)


def format_signal_id(source_channel_id: str, source_message_id: str) -> str:
    try:
        channel_id = abs(int(source_channel_id))
    except ValueError:
        return f"{source_channel_id}:{source_message_id}"
    return f"sig-c{channel_id}-m{source_message_id}"


def map_pair_to_freqtrade(pair_raw: str) -> str:
    cleaned = pair_raw.strip().upper().replace("$", "").replace("#", "")
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


def classify_message_type(raw_text: str, media: list[dict[str, Any]] | None = None) -> MessageType:
    text = raw_text.strip()
    if not text:
        if media:
            return MessageType.POSITION_SCREENSHOT
        return MessageType.NOISE

    headline = _first_nonempty_line(text)
    if re.search(UPDATE_HEADLINE_PATTERN, headline, re.IGNORECASE):
        return MessageType.UPDATE

    if _looks_like_new_signal(text, headline):
        return MessageType.NEW_SIGNAL

    return MessageType.NOISE


def _first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _looks_like_new_signal(text: str, headline: str) -> bool:
    has_buy_plan = bool(re.search(BUY_PLAN_PATTERN, headline, re.IGNORECASE))
    has_entry = bool(re.search(ENTRY_LABEL_PATTERN, text, re.IGNORECASE))
    has_risk = bool(re.search(RISK_LABEL_PATTERN, text, re.IGNORECASE))
    has_profit = bool(re.search(PROFIT_LABEL_PATTERN, text, re.IGNORECASE))
    return has_buy_plan and has_entry and has_risk and has_profit


def parse_signal(
    raw_message: str | dict[str, Any],
    pair_whitelist: set[str] | None = None,
    current_prices: dict[str, float] | None = None,
    risk_policy: RiskPolicy | None = None,
) -> TradingSignal:
    parser = SignalParser(pair_whitelist=pair_whitelist, risk_policy=risk_policy)
    return parser.parse(raw_message, current_prices=current_prices)


def extract_directives(
    message_text: str,
    pair: str,
    message_id: int | None = None,
) -> list[PositionDirective]:
    try:
        text = str(message_text or "")
        directives: list[PositionDirective] = []

        if _has_close_directive(text):
            directives.append(
                PositionDirective(
                    kind=DirectiveKind.CLOSE,
                    pair=pair,
                    source_message_id=message_id,
                )
            )

        partial_fraction = _partial_close_fraction(text)
        if partial_fraction is not None:
            directives.append(
                PositionDirective(
                    kind=DirectiveKind.PARTIAL_CLOSE,
                    pair=pair,
                    fraction=partial_fraction,
                    source_message_id=message_id,
                )
            )

        if _has_any_pattern(text, MOVE_SL_TO_ENTRY_PATTERNS):
            directives.append(
                PositionDirective(
                    kind=DirectiveKind.MOVE_SL_TO_ENTRY,
                    pair=pair,
                    source_message_id=message_id,
                )
            )

        move_sl_price = _move_sl_price(text)
        if move_sl_price is not None:
            directives.append(
                PositionDirective(
                    kind=DirectiveKind.MOVE_SL,
                    pair=pair,
                    price=move_sl_price,
                    source_message_id=message_id,
                )
            )

        return directives
    except Exception:
        return []


def _has_close_directive(text: str) -> bool:
    close_patterns = (
        r"正在平仓",
        r"平仓播报",
        r"会员们.*平仓",
        r"\bclose\s+position\b",
        r"\bclosed\b",
        r"\bfull\s+close\b",
    )
    return _has_any_pattern(text, close_patterns)


def _partial_close_fraction(text: str) -> float | None:
    for pattern in PARTIAL_DIRECTIVE_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        window = text[match.start() : min(len(text), match.end() + 20)]
        fraction = _extract_fraction(window)
        if fraction is not None:
            return fraction
        window = text[max(0, match.start() - 20) : min(len(text), match.end() + 20)]
        fraction = _extract_fraction(window)
        if fraction is not None:
            return fraction
        if _has_any_pattern(window, PARTIAL_DEFAULT_PATTERNS):
            return 0.5
    return None


def _extract_fraction(text: str) -> float | None:
    for match in re.finditer(r"(?<!近)(\d+(?:\.\d+)?)\s*%", text):
        value = float(match.group(1))
        if value <= 0 or value > 100:
            continue
        return value / 100.0
    return None


def _move_sl_price(text: str) -> float | None:
    for pattern in MOVE_SL_PRICE_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        return float(match.group(1))
    return None


def _has_any_pattern(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE | re.DOTALL) for pattern in patterns)


class SignalParser:
    def __init__(
        self,
        pair_whitelist: set[str] | None = None,
        risk_policy: RiskPolicy | None = None,
    ) -> None:
        if pair_whitelist:
            self.pair_whitelist = pair_whitelist
        else:
            self.pair_whitelist = set()

        if risk_policy:
            self.risk_policy = risk_policy
        else:
            self.risk_policy = RiskPolicy()

    def parse(
        self,
        raw_message: str | dict[str, Any],
        current_prices: dict[str, float] | None = None,
    ) -> TradingSignal:
        message = self._normalize_message(raw_message)
        raw_text = message["raw_text"]
        pair_raw = self._extract_pair_raw(raw_text)
        pair_freqtrade = map_pair_to_freqtrade(pair_raw)
        side = self._extract_side(raw_text)
        received_at = parse_utc_datetime(message["received_at"])
        entry = self._extract_entry(raw_text, received_at)
        leverage = self._extract_leverage(raw_text)
        take_profits = self._extract_take_profits(raw_text)
        stop_loss = self._extract_stop_loss(raw_text)
        media = [MediaAsset(**item) for item in message["media"]]

        signal = TradingSignal(
            signal_id=message["signal_id"],
            source=message["source"],
            source_channel_id=message["source_channel_id"],
            source_channel_name=message["source_channel_name"],
            source_message_id=message["source_message_id"],
            received_at=received_at,
            raw_text=raw_text,
            media=media,
            parser_version=PARSER_VERSION,
            message_type=message["message_type"],
            pair_raw=pair_raw,
            pair_freqtrade=pair_freqtrade,
            side=side,
            entry=entry,
            stop_loss=stop_loss,
            take_profits=take_profits,
            take_profit_parse_status=self._take_profit_parse_status(raw_text, take_profits),
            leverage=leverage,
            confidence=self._confidence(pair_raw, side, stop_loss, take_profits),
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
                "received_at": None,
                "raw_text": raw_message,
                "media": [],
                "message_type": classify_message_type(raw_message),
            }

        source_channel_id = str(raw_message.get("source_channel_id", "manual"))
        source_message_id = str(raw_message.get("source_message_id", "0"))
        signal_id = raw_message.get("signal_id")
        if not signal_id:
            signal_id = format_signal_id(source_channel_id, source_message_id)

        raw_text = str(raw_message.get("raw_text", ""))
        media = list(raw_message.get("media", []))
        message_type = raw_message.get("message_type")
        if not message_type:
            message_type = classify_message_type(raw_text, media)
        else:
            message_type = MessageType(str(getattr(message_type, "value", message_type)))

        return {
            "signal_id": str(signal_id),
            "source": str(raw_message.get("source", "manual")),
            "source_channel_id": source_channel_id,
            "source_channel_name": str(raw_message.get("source_channel_name", "")),
            "source_message_id": source_message_id,
            "received_at": raw_message.get("received_at"),
            "raw_text": raw_text,
            "media": media,
            "message_type": message_type,
        }

    def _extract_pair_raw(self, raw_text: str) -> str:
        for alias, pair in PAIR_ALIASES:
            if alias in raw_text:
                return pair

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
            if token.isdigit():
                continue
            if token.endswith("USDT"):
                return token
            return f"{token}USDT"
        return "UNKNOWNUSDT"

    def _extract_side(self, raw_text: str) -> str:
        if re.search(r"\bLONG\b|做多|多单", raw_text, re.IGNORECASE):
            return "long"
        if re.search(r"\bSHORT\b|空单|空头|做空", raw_text, re.IGNORECASE):
            return "short"
        return "long"

    def _extract_entry(self, raw_text: str, received_at) -> EntryPlan:
        dca_prices = self._extract_prices_after_label(raw_text, r"DCA|补仓|定投(?:入场|开单)?(?:点)?")
        primary_price = self._extract_primary_price(raw_text)
        mode = "cmp"
        price_max = self._extract_cmp_entry_upper_bound(raw_text)

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
            price_max=price_max if has_cmp else None,
            dca_prices=dca_prices,
            valid_from=received_at,
            expires_at=received_at + timedelta(minutes=self.risk_policy.signal_max_age_minutes),
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
                if not re.search(r"till|until|至|到", line, re.IGNORECASE):
                    prices = self._prices_from_line(line)
                    if prices:
                        return prices[0]
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
        return [TakeProfit(price=price, close_pct=100) for price in prices]

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
        prices = []
        for match in re.finditer(
            r"[$￥]?\s*([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+(?:\.[0-9]+)?)(?![0-9.]|\s*%)\s*(万)?",
            line,
        ):
            price = float(match.group(1).replace(",", ""))
            if match.group(2):
                price *= 10000
            prices.append(price)
        return prices

    def _extract_leverage(self, raw_text: str) -> LeveragePlan:
        match = re.search(
            r"(\d+)\s*(?:倍|x|X)\s*(?:-|~|至|到)\s*(\d+)\s*(?:倍|x|X)",
            raw_text,
        )
        if match:
            min_leverage = int(match.group(1))
            max_leverage = int(match.group(2))
            selected = min(self.risk_policy.default_leverage, max_leverage)
            selected = max(selected, min_leverage)
            return LeveragePlan(min=min_leverage, max=max_leverage, selected=selected)

        selected = self.risk_policy.default_leverage
        return LeveragePlan(min=selected, max=selected, selected=selected)

    def _take_profit_parse_status(self, raw_text: str, take_profits: list[TakeProfit]) -> str:
        if take_profits:
            return "parsed"
        if re.search(r"\bTP\b|止盈", raw_text, re.IGNORECASE):
            return "missing_explicit_price"
        return "missing"

    def _confidence(
        self,
        pair_raw: str,
        side: str,
        stop_loss: float | None,
        take_profits: list[TakeProfit],
    ) -> float:
        score = 0.35
        if pair_raw != "UNKNOWNUSDT":
            score += 0.2
        if side in {"long", "short"}:
            score += 0.15
        if stop_loss:
            score += 0.15
        if take_profits:
            score += 0.15
        return min(score, 0.95)

    def _apply_review_reasons(
        self,
        signal: TradingSignal,
        current_prices: dict[str, float] | None,
    ) -> None:
        reason_codes: list[str] = []
        if self.pair_whitelist and signal.pair_freqtrade not in self.pair_whitelist:
            reason_codes.append("pair_not_whitelisted")

        if signal.stop_loss is None:
            reason_codes.append("stop_loss_missing")

        geometry_prices: dict[str, float] = {}
        if current_prices:
            geometry_prices.update(current_prices)
        price_hint = self._extract_cmp_price_hint(signal)
        if price_hint is not None and signal.pair_freqtrade not in geometry_prices:
            geometry_prices[signal.pair_freqtrade] = price_hint

        geometry_codes = self._geometry_review_reasons(signal, geometry_prices)
        reason_codes.extend(geometry_codes)

        signal.review_reason_codes = sorted(set(reason_codes))
        if signal.review_reason_codes:
            signal.status = SignalStatus.NEEDS_REVIEW
            signal.risk_policy_result = RiskPolicyResult(
                decision="blocked",
                reason_codes=["manual_review_required"],
            )

    def _geometry_review_reasons(
        self,
        signal: TradingSignal,
        current_prices: dict[str, float] | None,
    ) -> list[str]:
        if not current_prices:
            return []

        entry_price = signal.entry.primary_price
        if entry_price is None:
            entry_price = current_prices.get(signal.pair_freqtrade)
        if entry_price is None:
            return []

        take_profit_prices = [take_profit.price for take_profit in signal.take_profits]
        if not take_profit_prices:
            return []

        governor = RiskGovernor(self.risk_policy)
        result = governor.validate_price_geometry(
            signal.side,
            entry_price,
            signal.stop_loss,
            take_profit_prices,
        )
        return result.reason_codes

    def _extract_cmp_price_hint(self, signal: TradingSignal) -> float | None:
        for line in signal.raw_text.splitlines():
            if not self._has_cmp_text(line):
                continue
            if re.search(r"DCA|补仓|定投", line, re.IGNORECASE):
                continue
            match = re.search(
                r"(?:till|until|至|到|价格)\s*[$￥]?\s*([0-9]+(?:\.[0-9]+)?)",
                line,
                re.IGNORECASE,
            )
            if not match:
                continue
            return self._normalize_cmp_price_hint(float(match.group(1)), signal)
        return None

    def _extract_cmp_entry_upper_bound(self, raw_text: str) -> float | None:
        for line in raw_text.splitlines():
            if not self._has_cmp_text(line):
                continue
            if re.search(r"DCA|补仓|定投", line, re.IGNORECASE):
                continue
            if not re.search(r"Entry|入场点|开单价|首次", line, re.IGNORECASE):
                continue
            match = re.search(
                r"(?:till|until|至|到|价格)\s*[$￥]?\s*([0-9]+(?:\.[0-9]+)?)",
                line,
                re.IGNORECASE,
            )
            if match:
                return float(match.group(1))
        return None

    def _has_cmp_text(self, text: str) -> bool:
        return bool(
            re.search(
                r"(?<![A-Z0-9])CMP(?![A-Z0-9])|现价|市場价|市场价|当前市价|市价",
                text,
                re.IGNORECASE,
            )
        )

    def _normalize_cmp_price_hint(self, price: float, signal: TradingSignal) -> float:
        references = [take_profit.price for take_profit in signal.take_profits]
        if signal.stop_loss:
            references.append(signal.stop_loss)
        if not references:
            return price

        max_reference = max(references)
        normalized = price
        while normalized > 1 and max_reference < 1 and normalized > max_reference * 10:
            normalized = normalized / 10
        return normalized
