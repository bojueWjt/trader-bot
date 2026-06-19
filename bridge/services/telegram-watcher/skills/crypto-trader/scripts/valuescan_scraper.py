#!/usr/bin/env python3
"""
ValueScan (valuescan.io) 数据爬虫
用于抓取主力资金流、AI异动追踪、大众情绪数据

用法:
    python3 valuescan_scraper.py scan BTC ETH DOT XRP HYPE
    python3 valuescan_scraper.py report BTC ETH
"""

import argparse
import json
import subprocess
import sys
import time
import re
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from pathlib import Path

# ValueScan keyword 映射（通过观察获取）
TOKEN_KEYWORD_MAP = {
    "BTC": "1",
    "ETH": "2",
    "BNB": "3",
    "XRP": "4",
    "DOT": "7",
    "ADA": "8",
    "SOL": "9",
    "AVAX": "11",
    "MATIC": "12",
    "LINK": "13",
    "UNI": "15",
    "LTC": "18",
    "ATOM": "19",
    "ETC": "21",
    "XLM": "22",
    "ALGO": "23",
    "FIL": "28",
    "TRX": "29",
    "EOS": "31",
    "AAVE": "36",
    "XTZ": "40",
    "NEO": "43",
    "DASH": "46",
    "XMR": "51",
    "IOTA": "52",
    "ZEC": "55",
    "WAVES": "58",
    "OMG": "59",
    "ZRX": "65",
    "KNC": "67",
    "BAT": "70",
    "ICX": "74",
    "ENJ": "77",
    "ZIL": "79",
    "VET": "81",
    "THETA": "83",
    "DOGE": "88",
    "HYPE": "1000",  # 需要确认
}

AGENT_BROWSER_PROFILE = Path.home() / ".valuescan-profile"
AGENT_BROWSER_TIMEOUT = 30


@dataclass
class SpotFund:
    """现货资金数据（单位：万美元）"""
    m5: float = 0.0
    m15: float = 0.0
    m30: float = 0.0
    h1: float = 0.0
    h4: float = 0.0
    h8: float = 0.0
    h12: float = 0.0
    h24: float = 0.0


@dataclass
class ContractFund:
    """合约资金数据（单位：万美元）"""
    m5: float = 0.0
    m15: float = 0.0
    m30: float = 0.0
    h1: float = 0.0
    h4: float = 0.0
    h8: float = 0.0
    h12: float = 0.0
    h24: float = 0.0


@dataclass
class Alerts24H:
    """24小时AI异动数据"""
    total: int = 0
    long_inflow: int = 0      # Long Inflow Alert
    short_inflow: int = 0     # Short Inflow Alert
    uptrend_weak: int = 0     # Uptrend Weakening
    fomo: int = 0             # Volume Surge / FOMO Alert
    capital_shift: int = 0    # Capital Protection/Shift


@dataclass
class Sentiment:
    """大众情绪数据"""
    long: float = 0.0
    neutral: float = 0.0
    short: float = 0.0


@dataclass
class TokenData:
    """单个币种的完整数据"""
    symbol: str
    price: float = 0.0
    price_change_24h: float = 0.0
    spot_fund: SpotFund = None
    contract_fund: ContractFund = None
    alerts_24h: Alerts24H = None
    latest_alert: str = ""
    sentiment: Sentiment = None
    fm_cap_ratio: str = ""
    login_required: bool = False
    limited_data: bool = False
    error: str = ""

    def __post_init__(self):
        if self.spot_fund is None:
            self.spot_fund = SpotFund()
        if self.contract_fund is None:
            self.contract_fund = ContractFund()
        if self.alerts_24h is None:
            self.alerts_24h = Alerts24H()
        if self.sentiment is None:
            self.sentiment = Sentiment()


def run_agent_browser(args: List[str], timeout: int = AGENT_BROWSER_TIMEOUT) -> tuple[int, str, str]:
    """运行 agent-browser CLI 命令"""
    cmd = ["agent-browser", "--profile", str(AGENT_BROWSER_PROFILE)] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"
    except Exception as e:
        return -1, "", str(e)


def ensure_profile_exists():
    """确保 browser profile 目录存在"""
    AGENT_BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)


def parse_money_value(text: str) -> float:
    """解析资金数值，返回万美元为单位"""
    if not text or text in ["—", "-", "", "--"]:
        return 0.0

    text = text.strip().replace(",", "").replace(" ", "")

    # 处理单位
    multiplier = 1.0
    if "K" in text.upper():
        multiplier = 0.1  # 千美元 -> 万美元
        text = text.upper().replace("K", "")
    elif "M" in text.upper():
        multiplier = 100  # 百万美元 -> 万美元
        text = text.upper().replace("M", "")
    elif "B" in text.upper():
        multiplier = 100000  # 十亿美元 -> 万美元
        text = text.upper().replace("B", "")
    elif "万" in text:
        multiplier = 1.0
        text = text.replace("万", "")
    elif "亿" in text:
        multiplier = 10000
        text = text.replace("亿", "")

    # 处理正负号
    sign = 1.0
    if text.startswith("+"):
        text = text[1:]
    elif text.startswith("-"):
        sign = -1.0
        text = text[1:]

    try:
        return float(text) * multiplier * sign
    except ValueError:
        return 0.0


def parse_percentage(text: str) -> float:
    """解析百分比数值"""
    if not text:
        return 0.0
    text = text.strip().replace("%", "").replace(" ", "")
    try:
        return float(text)
    except ValueError:
        return 0.0


def parse_price_change(text: str) -> float:
    """解析24小时涨跌百分比"""
    # 匹配格式: "1D Rise and Fall 0.68%" 或 "24H change: 3.98%"
    match = re.search(r'(?:Rise and Fall|change)[:\s]+([+-]?\d+\.?\d*)%', text)
    if match:
        return float(match.group(1))
    return 0.0


def check_limited_access(snapshot_text: str) -> bool:
    """检查是否只有有限数据访问（未登录）"""
    limited_indicators = [
        "Login to Learn More",
        "Your can now view signals from up to 8H ago",
        "view signals from up to 8H",
        "Login",
        "limited",
        "限制",
    ]
    text_lower = snapshot_text.lower()
    return any(ind.lower() in text_lower for ind in limited_indicators)


def extract_price_info(snapshot_text: str) -> tuple[float, float]:
    """提取价格和24小时涨跌"""
    price = 0.0
    change = 0.0

    # 查找主价格: "BTC $70658.88"
    price_match = re.search(r'(?:BTC|ETH|BNB|XRP|DOT)\s+\$?([\d,]+\.?\d*)', snapshot_text)
    if price_match:
        price_str = price_match.group(1).replace(",", "")
        try:
            price = float(price_str)
        except ValueError:
            pass

    # 查找24小时涨跌
    change_match = re.search(r'1D\s+Rise\s+and\s+Fall\s+([+-]?\d+\.?\d*)%', snapshot_text)
    if change_match:
        change = float(change_match.group(1))

    return price, change


def extract_table_fund_data(snapshot_text: str, table_name: str) -> Dict[str, float]:
    """
    从 snapshot 中提取资金表格数据

    Args:
        snapshot_text: agent-browser snapshot 的文本
        table_name: 表格名称（"Spot Fund" 或 "Futures Fund")

    Returns:
        各时间维度的净流入字典
    """
    data = {}

    # 查找表格区域
    table_start = snapshot_text.find(table_name)
    if table_start == -1:
        return data

    # 提取表格区域（接下来的2000字符）
    table_section = snapshot_text[table_start:table_start + 2000]

    # 时间维度映射
    timeframe_map = {
        "5m": "m5",
        "15m": "m15",
        "30m": "m30",
        "1H": "h1",
        "4H": "h4",
        "8H": "h8",
        "12H": "h12",
        "24H": "h24",
    }

    # 匹配行格式: row "5m 6.32M 14.69M -8.37M 74.28% 0.0₃59%":
    # 列: Time, Inflow, Outflow, Net Inflow, Net Inflow Change, F/M Cap Ratio
    row_pattern = r'row\s+"(5m|15m|30m|1H|4H|8H|12H|24H)\s+([^"]+)"'

    for match in re.finditer(row_pattern, table_section):
        tf_label = match.group(1)
        tf_key = timeframe_map.get(tf_label)
        if not tf_key:
            continue

        # 提取行内所有数值
        row_content = match.group(2)
        # 格式: "6.32M 14.69M -8.37M 74.28% 0.0₃59%"
        # 解析各个字段
        parts = row_content.split()
        if len(parts) >= 3:
            # parts[0] = Inflow, parts[1] = Outflow, parts[2] = Net Inflow
            net_inflow = parse_money_value(parts[2])
            data[tf_key] = net_inflow

    return data


def extract_ai_alerts(snapshot_text: str) -> tuple[Alerts24H, str]:
    """提取 AI 异动追踪数据"""
    alerts = Alerts24H()
    latest_alert = ""

    # 查找 AI Tracking 区域
    ai_start = snapshot_text.find("AI Tracking")
    if ai_start == -1:
        return alerts, latest_alert

    ai_section = snapshot_text[ai_start:ai_start + 5000]

    # 统计各类异动
    alerts.long_inflow = ai_section.count("Long Inflow Alert")
    alerts.short_inflow = ai_section.count("Short Inflow Alert")
    alerts.uptrend_weak = ai_section.count("Uptrend Weakening")
    alerts.fomo = ai_section.count("Volume Surge") + ai_section.count("FOMO Alert")
    alerts.capital_shift = ai_section.count("Capital Protection") + ai_section.count("Funds Shift")

    alerts.total = alerts.long_inflow + alerts.short_inflow + alerts.uptrend_weak + alerts.fomo + alerts.capital_shift

    # 提取最新异动（通常是第一个完整的 alert）
    # 格式: "02/09 16:00 Long Inflow Alert BTC Uptrend Weakening..."
    alert_pattern = r'(\d{2}/\d{2}\s+\d{2}:\d{2}\s+[^"]+?)(?:\s+\d{2}/\d{2}|"|$)'
    match = re.search(alert_pattern, ai_section)
    if match:
        latest_alert = match.group(1).strip()
        # 限制长度
        if len(latest_alert) > 100:
            latest_alert = latest_alert[:97] + "..."

    return alerts, latest_alert


def extract_sentiment(snapshot_text: str) -> Sentiment:
    """提取大众情绪数据"""
    sentiment = Sentiment()

    # 查找情绪数据: "Long 40.3% Neutral 51.46% Short 8.24%"
    pattern = r'Long\s+([\d.]+)%\s+Neutral\s+([\d.]+)%\s+Short\s+([\d.]+)%'
    match = re.search(pattern, snapshot_text)

    if match:
        sentiment.long = float(match.group(1))
        sentiment.neutral = float(match.group(2))
        sentiment.short = float(match.group(3))

    return sentiment


def extract_fm_cap_ratio(snapshot_text: str) -> str:
    """提取 F/M Cap Ratio"""
    # 查找最近的 F/M Cap Ratio 值（通常是30m的）
    pattern = r'cell\s+"0\.0[₀₁₂₃₄₅₆₇₈₉]+?%"'
    matches = re.findall(pattern, snapshot_text)
    if matches:
        # 返回第一个找到的非零值
        for match in matches:
            value = match.replace('cell "', '').replace('"', '')
            if value != "0.0000%":
                return value
    return ""


def scrape_token(symbol: str) -> TokenData:
    """抓取单个币种的数据"""
    symbol = symbol.upper()
    data = TokenData(symbol=symbol)

    # 获取 keyword
    keyword = TOKEN_KEYWORD_MAP.get(symbol)
    if not keyword:
        data.error = f"Unknown token mapping for {symbol}, please add to TOKEN_KEYWORD_MAP"
        return data

    url = f"https://www.valuescan.io/token?keyword={keyword}"

    # 打开页面
    returncode, stdout, stderr = run_agent_browser(["open", url], timeout=15)
    if returncode != 0:
        data.error = f"Failed to open page: {stderr}"
        return data

    time.sleep(2)  # 等待页面加载

    # 获取 snapshot
    returncode, stdout, stderr = run_agent_browser(["snapshot", "--format", "ai"], timeout=15)
    if returncode != 0:
        data.error = f"Failed to get snapshot: {stderr}"
        return data

    snapshot_text = stdout

    # 检查是否有限数据访问
    data.limited_data = check_limited_access(snapshot_text)
    if data.limited_data:
        data.login_required = True

    # 提取价格
    data.price, data.price_change_24h = extract_price_info(snapshot_text)

    # 提取现货资金数据
    spot_data = extract_table_fund_data(snapshot_text, "Spot Fund")
    data.spot_fund.m5 = spot_data.get("m5", 0)
    data.spot_fund.m15 = spot_data.get("m15", 0)
    data.spot_fund.m30 = spot_data.get("m30", 0)
    data.spot_fund.h1 = spot_data.get("h1", 0)
    data.spot_fund.h4 = spot_data.get("h4", 0)
    data.spot_fund.h8 = spot_data.get("h8", 0)
    data.spot_fund.h12 = spot_data.get("h12", 0)
    data.spot_fund.h24 = spot_data.get("h24", 0)

    # 提取合约资金数据
    contract_data = extract_table_fund_data(snapshot_text, "Futures Fund")
    data.contract_fund.m5 = contract_data.get("m5", 0)
    data.contract_fund.m15 = contract_data.get("m15", 0)
    data.contract_fund.m30 = contract_data.get("m30", 0)
    data.contract_fund.h1 = contract_data.get("h1", 0)
    data.contract_fund.h4 = contract_data.get("h4", 0)
    data.contract_fund.h8 = contract_data.get("h8", 0)
    data.contract_fund.h12 = contract_data.get("h12", 0)
    data.contract_fund.h24 = contract_data.get("h24", 0)

    # 提取 AI 异动
    data.alerts_24h, data.latest_alert = extract_ai_alerts(snapshot_text)

    # 提取大众情绪
    data.sentiment = extract_sentiment(snapshot_text)

    # 提取 F/M Cap Ratio
    data.fm_cap_ratio = extract_fm_cap_ratio(snapshot_text)

    return data


def scan_tokens(symbols: List[str]) -> List[TokenData]:
    """批量扫描多个币种"""
    ensure_profile_exists()
    results = []

    for i, symbol in enumerate(symbols):
        if i > 0:
            time.sleep(1.5)  # 币种间延迟

        print(f"Scanning {symbol}...", file=sys.stderr)
        data = scrape_token(symbol)
        results.append(data)

    # 完成后关闭 browser
    run_agent_browser(["close"], timeout=5)

    return results


def format_fund_value(value: float) -> str:
    """格式化资金数值显示"""
    if value == 0:
        return "—"
    if abs(value) >= 10000:
        return f"{value/10000:+.1f}亿"
    elif abs(value) >= 1000:
        return f"{value/1000:+.1f}千万"
    else:
        return f"{value:+.0f}万"


def generate_report(results: List[TokenData]) -> str:
    """生成早报格式的文本报告"""
    lines = ["🔍 ValueScan 主力资金数据", ""]
    lines.append("┌──────────┬────────────┬────────────┬──────────┬────────────┐")
    lines.append("│ 币种     │ 现货30m    │ 合约30m    │ 异动24H  │ 大众情绪   │")
    lines.append("├──────────┼────────────┼────────────┼──────────┼────────────┤")

    for data in results:
        symbol = data.symbol.ljust(8)

        # 现货30m净流入
        spot_str = format_fund_value(data.spot_fund.m30).ljust(10)

        # 合约30m净流入
        contract_str = format_fund_value(data.contract_fund.m30).ljust(10)

        # 异动次数（24H内统计）
        total_alerts = data.alerts_24h.total
        if total_alerts > 10:
            alert_str = f"🔥{total_alerts}次".ljust(8)
        elif total_alerts > 5:
            alert_str = f"🟢{total_alerts}次".ljust(8)
        elif total_alerts > 0:
            alert_str = f"🟡{total_alerts}次".ljust(8)
        else:
            alert_str = "⚪—".ljust(8)

        # 大众情绪
        if data.sentiment.long > 50:
            sentiment_str = "🚀看涨".ljust(10)
        elif data.sentiment.short > 30:
            sentiment_str = "🔻看跌".ljust(10)
        elif data.sentiment.long > data.sentiment.short + 10:
            sentiment_str = "📈偏多".ljust(10)
        elif data.sentiment.short > data.sentiment.long + 10:
            sentiment_str = "📉偏空".ljust(10)
        elif data.sentiment.long > 0:
            sentiment_str = "😐中性".ljust(10)
        else:
            sentiment_str = "—".ljust(10)

        lines.append(f"│ {symbol}│ {spot_str}│ {contract_str}│ {alert_str}│ {sentiment_str}│")

    lines.append("└──────────┴────────────┴────────────┴──────────┴────────────┘")
    lines.append("")

    # 详细情绪数据
    has_sentiment = any(r.sentiment.long > 0 for r in results)
    if has_sentiment:
        lines.append("😊 大众情绪")
        for data in results:
            if data.sentiment.long > 0:
                lines.append(f"• {data.symbol}: 看涨 {data.sentiment.long:.1f}% | 中立 {data.sentiment.neutral:.1f}% | 看跌 {data.sentiment.short:.1f}%")
        lines.append("")

    # AI异动详情
    has_alerts = any(r.alerts_24h.total > 0 for r in results)
    if has_alerts:
        lines.append("⚡ AI异动追踪 (24H)")
        for data in results:
            if data.alerts_24h.total > 0:
                alert_types = []
                if data.alerts_24h.long_inflow > 0:
                    alert_types.append(f"流入{data.alerts_24h.long_inflow}")
                if data.alerts_24h.uptrend_weak > 0:
                    alert_types.append(f"转弱{data.alerts_24h.uptrend_weak}")
                if data.alerts_24h.fomo > 0:
                    alert_types.append(f"FOMO{data.alerts_24h.fomo}")
                if data.alerts_24h.short_inflow > 0:
                    alert_types.append(f"流出{data.alerts_24h.short_inflow}")

                alert_type_str = "/".join(alert_types)
                lines.append(f"• {data.symbol}: 共{data.alerts_24h.total}次 ({alert_type_str})")
                if data.latest_alert:
                    lines.append(f"  最新: {data.latest_alert[:60]}...")
        lines.append("")

    # 登录提示
    need_login = any(r.limited_data for r in results)
    if need_login:
        lines.append("⚠️ 提示: 当前未登录 ValueScan，仅显示30分钟数据。登录后可查看更长时间维度。")
        lines.append("   如需完整数据，请运行: agent-browser --profile ~/.valuescan-profile --headed open https://www.valuescan.io")

    return '\n'.join(lines)


def results_to_dict(results: List[TokenData]) -> List[Dict]:
    """将结果转换为可序列化的字典列表"""
    output = []
    for data in results:
        output.append({
            "symbol": data.symbol,
            "price": data.price,
            "price_change_24h": data.price_change_24h,
            "spot_fund": {
                "m5": data.spot_fund.m5,
                "m15": data.spot_fund.m15,
                "m30": data.spot_fund.m30,
                "h1": data.spot_fund.h1,
                "h4": data.spot_fund.h4,
                "h8": data.spot_fund.h8,
                "h12": data.spot_fund.h12,
                "h24": data.spot_fund.h24,
            },
            "contract_fund": {
                "m5": data.contract_fund.m5,
                "m15": data.contract_fund.m15,
                "m30": data.contract_fund.m30,
                "h1": data.contract_fund.h1,
                "h4": data.contract_fund.h4,
                "h8": data.contract_fund.h8,
                "h12": data.contract_fund.h12,
                "h24": data.contract_fund.h24,
            },
            "alerts_24h": {
                "total": data.alerts_24h.total,
                "long_inflow": data.alerts_24h.long_inflow,
                "short_inflow": data.alerts_24h.short_inflow,
                "uptrend_weakening": data.alerts_24h.uptrend_weak,
                "fomo": data.alerts_24h.fomo,
                "capital_shift": data.alerts_24h.capital_shift,
            },
            "latest_alert": data.latest_alert,
            "sentiment": {
                "long": data.sentiment.long,
                "neutral": data.sentiment.neutral,
                "short": data.sentiment.short,
            },
            "fm_cap_ratio": data.fm_cap_ratio,
            "limited_data": data.limited_data,
            "login_required": data.login_required,
            "error": data.error,
        })
    return output


def main():
    parser = argparse.ArgumentParser(
        description="ValueScan 数据爬虫 - 抓取主力资金、AI异动、大众情绪",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 扫描单个币种
    python3 valuescan_scraper.py scan BTC

    # 批量扫描
    python3 valuescan_scraper.py scan BTC ETH DOT XRP HYPE

    # 生成早报格式报告
    python3 valuescan_scraper.py report BTC ETH DOT XRP HYPE

    # 查看/添加币种映射
    python3 valuescan_scraper.py map
    python3 valuescan_scraper.py map --add HYPE 1000
        """
    )

    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # scan 命令
    scan_parser = subparsers.add_parser("scan", help="扫描币种数据，输出JSON")
    scan_parser.add_argument(
        "symbols",
        nargs="+",
        help="要扫描的币种符号（如 BTC ETH）"
    )

    # report 命令
    report_parser = subparsers.add_parser("report", help="生成早报格式报告")
    report_parser.add_argument(
        "symbols",
        nargs="+",
        help="要报告的币种符号"
    )

    # map 命令
    map_parser = subparsers.add_parser("map", help="显示/添加币种 keyword 映射")
    map_parser.add_argument("--add", nargs=2, metavar=("SYMBOL", "KEYWORD"),
                           help="添加新的映射，如: --add HYPE 1000")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "map":
        if args.add:
            symbol, keyword = args.add
            TOKEN_KEYWORD_MAP[symbol.upper()] = keyword
            print(f"✅ 已添加映射: {symbol.upper()} -> {keyword}")
            print("\n当前所有映射:")
            for k, v in sorted(TOKEN_KEYWORD_MAP.items()):
                print(f"  {k}: {v}")
        else:
            print("ValueScan 币种 keyword 映射表:")
            print("-" * 30)
            for symbol, keyword in sorted(TOKEN_KEYWORD_MAP.items()):
                print(f"  {symbol:8} → {keyword}")
            print("-" * 30)
            print(f"\n共 {len(TOKEN_KEYWORD_MAP)} 个币种")
            print("\n提示: 如需添加新币种，使用:")
            print("  python3 valuescan_scraper.py map --add SYMBOL KEYWORD")
        sys.exit(0)

    if args.command == "scan":
        results = scan_tokens(args.symbols)
        output = results_to_dict(results)
        print(json.dumps(output, indent=2, ensure_ascii=False))
        sys.exit(0)

    if args.command == "report":
        results = scan_tokens(args.symbols)
        report = generate_report(results)
        print(report)
        sys.exit(0)


if __name__ == "__main__":
    main()
