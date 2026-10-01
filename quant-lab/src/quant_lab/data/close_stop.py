"""Conservative, direction-independent mapping of quoted close-stop conditions."""
from decimal import Decimal
import re
from typing import Literal, TypedDict

Timeframe = Literal["15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "1w"]


class CloseStop(TypedDict):
    level: Decimal
    timeframe: Timeframe


_CLOSE = re.compile(r"关闭|關閉|收盘|收於|收于|收线|收線|\bclos(?:e|es|ing)\b", re.I)
_PATTERNS = {
    "15m": r"(?<![a-z0-9一二三四五六七八九十百两兩])(?:m15|15\s*(?:m(?:in(?:ute)?s?)?|分钟|分鐘)|十五\s*(?:分钟|分鐘))(?![a-z0-9])",
    "30m": r"(?<![a-z0-9一二三四五六七八九十百两兩])(?:m30|30\s*(?:m(?:in(?:ute)?s?)?|分钟|分鐘)|三十\s*(?:分钟|分鐘)|半小时)(?![a-z0-9])",
    "1h": r"(?<![a-z0-9一二三四五六七八九十百两兩])(?:h1|1\s*(?:h(?:ou)?rs?|h|小时|小時)|一\s*(?:小时|小時)|小时线|小時線|hourly)(?![a-z0-9])",
    "2h": r"(?<![a-z0-9一二三四五六七八九十百两兩])(?:h2|2\s*(?:h(?:ou)?rs?|h|小时|小時)|[二两兩]\s*(?:小时|小時))(?![a-z0-9])",
    "4h": r"(?<![a-z0-9一二三四五六七八九十百两兩])(?:h4|4\s*(?:h(?:ou)?rs?|h|小时|小時)|四\s*(?:小时|小時))(?![a-z0-9])",
    "6h": r"(?<![a-z0-9一二三四五六七八九十百两兩])(?:h6|6\s*(?:h(?:ou)?rs?|h|小时|小時)|六\s*(?:小时|小時))(?![a-z0-9])",
    "12h": r"(?<![a-z0-9一二三四五六七八九十百两兩])(?:h12|12\s*(?:h(?:ou)?rs?|h|小时|小時)|十二\s*(?:小时|小時))(?![a-z0-9])",
    "1d": r"(?<![a-z0-9一二三四五六七八九十百两兩])(?:1\s*d(?:ay)?|d|daily|每日|每天|日线|日線|日\s*k)(?![a-z0-9])",
    "1w": r"(?<![a-z0-9一二三四五六七八九十百两兩])(?:1\s*w(?:eek)?|w|weekly|周线|週線|周線|周\s*k|週\s*k)(?![a-z0-9])",
}
_PERIODS = {tf: re.compile(pattern, re.I) for tf, pattern in _PATTERNS.items()}


def parse_close_stop(condition: str | None, price: Decimal | None) -> CloseStop | None:
    """Use an already evidence-checked price, never extract or guess one from text."""
    if not isinstance(condition, str) or price is None or not price.is_finite() or price <= 0:
        return None
    if not _CLOSE.search(condition):
        return None
    periods = [tf for tf, pattern in _PERIODS.items() if pattern.search(condition)]
    if len(periods) != 1:
        return None
    return {"level": price, "timeframe": periods[0]}
