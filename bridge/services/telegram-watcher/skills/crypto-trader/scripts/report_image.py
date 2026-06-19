#!/usr/bin/env python3
"""
交易持仓图片渲染器

生成暗色主题的持仓概览图片，用于嵌入 Hexo 博客日报。

Usage:
    echo '<json>' | python3 report_image.py
    python3 report_image.py --input data.json
    python3 report_image.py --input data.json --output /path/to/output.png

输出到 Hexo source/report/images/ 目录，返回 JSON 含相对路径。
"""

import argparse
import json
import os
import sys
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

# --- Config ---
HEXO_REPORT_DIR = os.path.expanduser("~/.openclaw/workspace/blog/source/report")
IMG_DIR = os.path.join(HEXO_REPORT_DIR, "images")

# Colors
BG = (18, 18, 32)           # #121220
CARD_BG = (28, 28, 48)      # #1c1c30
HEADER_BG = (38, 38, 58)    # #26263a
TEXT = (220, 220, 230)       # light gray
TEXT_DIM = (140, 140, 160)   # dim gray
GREEN = (0, 210, 130)        # profit green
RED = (255, 75, 85)          # loss red
CYAN = (0, 200, 255)         # accent
YELLOW = (255, 200, 50)      # warning
WHITE = (255, 255, 255)
BORDER = (50, 50, 70)        # subtle border
LONG_BG = (0, 210, 130, 25)  # subtle green bg
SHORT_BG = (255, 75, 85, 25) # subtle red bg

# Font
FONT_PATH = "/System/Library/Fonts/SFNSMono.ttf"
FONT_FALLBACK = "/System/Library/Fonts/Menlo.ttc"


def get_font(size):
    """Load monospace font."""
    for path in [FONT_PATH, FONT_FALLBACK]:
        try:
            return ImageFont.truetype(path, size)
        except (IOError, OSError):
            continue
    return ImageFont.load_default()


def format_num(n, decimals=2):
    if n is None:
        return "-"
    if isinstance(n, int) or (isinstance(n, float) and n == int(n) and abs(n) > 100):
        return f"{int(n):,}"
    return f"{n:,.{decimals}f}"


def pnl_str(pnl):
    if pnl is None or pnl == 0:
        return "-"
    sign = "+" if pnl > 0 else ""
    return f"{sign}{format_num(pnl)}"


def pnl_color(pnl):
    if pnl is None or pnl == 0:
        return TEXT_DIM
    return GREEN if pnl > 0 else RED


def render_positions_image(data: dict, output_path: str) -> str:
    """Render positions + pending orders as a dark-themed PNG image."""

    positions = data.get("positions", [])
    pending = data.get("pending_orders", [])
    account = data.get("account", {})
    date = data.get("date", datetime.now().strftime("%Y-%m-%d"))

    font_sm = get_font(13)
    font_md = get_font(14)
    font_lg = get_font(16)
    font_title = get_font(18)

    # --- Layout ---
    padding = 20
    row_h = 32
    col_gap = 16

    # Columns: Symbol, Side, Qty, Entry, Current, SL, PnL
    cols = [
        ("Symbol", 110),
        ("Side", 70),
        ("Qty", 100),
        ("Entry", 100),
        ("Current", 100),
        ("SL", 100),
        ("PnL", 90),
    ]
    table_w = sum(c[1] for c in cols) + col_gap * (len(cols) - 1)
    img_w = table_w + padding * 2

    # Pending columns
    pcols = [
        ("Symbol", 110),
        ("Side", 70),
        ("Qty", 100),
        ("Price", 100),
    ]
    ptable_w = sum(c[1] for c in pcols) + col_gap * (len(pcols) - 1)
    img_w = max(img_w, ptable_w + padding * 2)

    # Calculate height
    y = padding
    # Account summary bar
    y += 36
    y += 12
    # Positions section
    y += 28  # title
    y += 8
    y += row_h  # header
    y += 2  # separator
    y += row_h * max(len(positions), 1)  # rows
    y += 20  # gap

    if pending:
        y += 28  # title
        y += 8
        y += row_h  # header
        y += 2
        y += row_h * len(pending)
        y += 20

    y += padding
    img_h = y

    # --- Draw ---
    img = Image.new("RGB", (img_w, img_h), BG)
    draw = ImageDraw.Draw(img)

    y = padding

    # Account summary bar
    bar_h = 36
    draw.rounded_rectangle(
        [padding - 4, y, img_w - padding + 4, y + bar_h],
        radius=8, fill=CARD_BG, outline=BORDER
    )
    balance = account.get("balance", 0)
    total_pnl = sum(p.get("pnl", 0) or 0 for p in positions)
    pnl_pct = (total_pnl / balance * 100) if balance > 0 else 0

    bal_text = f"Balance: ${format_num(balance)}"
    pnl_text = f"PnL: {pnl_str(total_pnl)} ({pnl_str(pnl_pct)}%)"
    date_text = date

    draw.text((padding + 12, y + 9), bal_text, fill=TEXT, font=font_md)
    draw.text((padding + 260, y + 9), pnl_text, fill=pnl_color(total_pnl), font=font_md)
    draw.text((img_w - padding - 100, y + 9), date_text, fill=TEXT_DIM, font=font_md)
    y += bar_h + 12

    # --- Positions ---
    draw.text((padding, y), "📈 Positions", fill=CYAN, font=font_title)
    y += 28 + 8

    # Header row
    draw.rounded_rectangle(
        [padding - 4, y - 4, img_w - padding + 4, y + row_h - 4],
        radius=6, fill=HEADER_BG
    )
    x = padding
    for name, w in cols:
        if name in ("Qty", "Entry", "Current", "SL", "PnL"):
            # Right-align numbers
            bbox = draw.textbbox((0, 0), name, font=font_sm)
            tw = bbox[2] - bbox[0]
            draw.text((x + w - tw, y + 8), name, fill=TEXT_DIM, font=font_sm)
        else:
            draw.text((x, y + 8), name, fill=TEXT_DIM, font=font_sm)
        x += w + col_gap
    y += row_h

    # Separator line
    draw.line([(padding, y), (img_w - padding, y)], fill=BORDER, width=1)
    y += 2

    # Data rows
    if positions:
        for i, p in enumerate(positions):
            # Alternate row bg
            if i % 2 == 0:
                draw.rounded_rectangle(
                    [padding - 4, y - 2, img_w - padding + 4, y + row_h - 4],
                    radius=4, fill=(24, 24, 42)
                )

            x = padding
            side = p["side"].upper()
            is_long = side in ("LONG", "BUY")
            side_color = GREEN if is_long else RED
            pnl_val = p.get("pnl")

            # Symbol
            draw.text((x, y + 8), p["symbol"], fill=WHITE, font=font_md)
            x += cols[0][1] + col_gap

            # Side with icon
            icon = "▲" if is_long else "▼"
            draw.text((x, y + 8), f"{icon} {side}", fill=side_color, font=font_md)
            x += cols[1][1] + col_gap

            # Qty (right-aligned)
            val = format_num(p["qty"], 4)
            bbox = draw.textbbox((0, 0), val, font=font_md)
            tw = bbox[2] - bbox[0]
            draw.text((x + cols[2][1] - tw, y + 8), val, fill=TEXT, font=font_md)
            x += cols[2][1] + col_gap

            # Entry
            val = format_num(p["entry"])
            bbox = draw.textbbox((0, 0), val, font=font_md)
            tw = bbox[2] - bbox[0]
            draw.text((x + cols[3][1] - tw, y + 8), val, fill=TEXT, font=font_md)
            x += cols[3][1] + col_gap

            # Current
            val = format_num(p.get("current", 0))
            bbox = draw.textbbox((0, 0), val, font=font_md)
            tw = bbox[2] - bbox[0]
            draw.text((x + cols[4][1] - tw, y + 8), val, fill=TEXT, font=font_md)
            x += cols[4][1] + col_gap

            # SL
            sl_val = p.get("sl")
            val = format_num(sl_val) if sl_val else "-"
            bbox = draw.textbbox((0, 0), val, font=font_md)
            tw = bbox[2] - bbox[0]
            draw.text((x + cols[5][1] - tw, y + 8), val, fill=YELLOW if sl_val else TEXT_DIM, font=font_md)
            x += cols[5][1] + col_gap

            # PnL
            val = pnl_str(pnl_val)
            bbox = draw.textbbox((0, 0), val, font=font_md)
            tw = bbox[2] - bbox[0]
            draw.text((x + cols[6][1] - tw, y + 8), val, fill=pnl_color(pnl_val), font=font_md)

            y += row_h
    else:
        draw.text((padding + 20, y + 8), "No open positions", fill=TEXT_DIM, font=font_md)
        y += row_h

    y += 20

    # --- Pending Orders ---
    if pending:
        draw.text((padding, y), "⏳ Pending Orders", fill=CYAN, font=font_title)
        y += 28 + 8

        # Header
        draw.rounded_rectangle(
            [padding - 4, y - 4, img_w - padding + 4, y + row_h - 4],
            radius=6, fill=HEADER_BG
        )
        x = padding
        for name, w in pcols:
            if name in ("Qty", "Price"):
                bbox = draw.textbbox((0, 0), name, font=font_sm)
                tw = bbox[2] - bbox[0]
                draw.text((x + w - tw, y + 8), name, fill=TEXT_DIM, font=font_sm)
            else:
                draw.text((x, y + 8), name, fill=TEXT_DIM, font=font_sm)
            x += w + col_gap
        y += row_h

        draw.line([(padding, y), (img_w - padding, y)], fill=BORDER, width=1)
        y += 2

        for i, o in enumerate(pending):
            if i % 2 == 0:
                draw.rounded_rectangle(
                    [padding - 4, y - 2, img_w - padding + 4, y + row_h - 4],
                    radius=4, fill=(24, 24, 42)
                )

            x = padding
            side = o["side"].upper()
            is_long = side in ("LONG", "BUY")
            side_color = GREEN if is_long else RED

            draw.text((x, y + 8), o["symbol"], fill=WHITE, font=font_md)
            x += pcols[0][1] + col_gap

            icon = "▲" if is_long else "▼"
            draw.text((x, y + 8), f"{icon} {side}", fill=side_color, font=font_md)
            x += pcols[1][1] + col_gap

            val = format_num(o["qty"], 4)
            bbox = draw.textbbox((0, 0), val, font=font_md)
            tw = bbox[2] - bbox[0]
            draw.text((x + pcols[2][1] - tw, y + 8), val, fill=TEXT, font=font_md)
            x += pcols[2][1] + col_gap

            val = format_num(o["price"])
            bbox = draw.textbbox((0, 0), val, font=font_md)
            tw = bbox[2] - bbox[0]
            draw.text((x + pcols[3][1] - tw, y + 8), val, fill=TEXT, font=font_md)

            y += row_h

    # Save
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    img.save(output_path, "PNG", optimize=True)
    return output_path


def main():
    parser = argparse.ArgumentParser(description="生成持仓概览图片")
    parser.add_argument("--input", "-i", default=None, help="JSON 输入文件（默认 stdin）")
    parser.add_argument("--output", "-o", default=None, help="输出 PNG 路径")
    args = parser.parse_args()

    if args.input:
        with open(args.input, "r") as f:
            data = json.load(f)
    else:
        data = json.load(sys.stdin)

    date = data.get("date", datetime.now().strftime("%Y-%m-%d"))

    if args.output:
        output = args.output
    else:
        os.makedirs(IMG_DIR, exist_ok=True)
        output = os.path.join(IMG_DIR, f"{date}-positions.png")

    render_positions_image(data, output)

    # Return relative path for Hexo markdown reference
    rel_path = f"/blog/report/images/{date}-positions.png"
    result = {
        "status": "ok",
        "output": output,
        "relative_path": rel_path,
        "markdown": f"![持仓概览]({rel_path})",
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
