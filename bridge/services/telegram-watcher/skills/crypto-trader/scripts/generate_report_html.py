#!/usr/bin/env python3
"""
generate_report_html.py — 将 Markdown 早报渲染为精美 HTML 并上传到服务器。

用法：
    echo "markdown content" | python3 generate_report_html.py
    python3 generate_report_html.py report.md
    python3 generate_report_html.py --date 2026-02-10 report.md

功能：
    1. 读取 Markdown 内容
    2. 渲染为带样式的 HTML（暗色主题，适配移动端）
    3. 上传到阿里云 /var/www/report/index.html
"""

import sys
import os
import subprocess
import datetime
import re

REMOTE = "root@39.108.225.60"
REMOTE_PATH = "/var/www/report/index.html"

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>交易日报 — {date}</title>
<style>
  :root {{
    --bg: #0d1117;
    --card: #161b22;
    --border: #30363d;
    --text: #e6edf3;
    --text2: #8b949e;
    --green: #3fb950;
    --red: #f85149;
    --blue: #58a6ff;
    --orange: #d29922;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.7;
    padding: 20px;
    max-width: 800px;
    margin: 0 auto;
  }}
  h1 {{
    font-size: 1.6em;
    margin: 20px 0 10px;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border);
  }}
  h2 {{
    font-size: 1.3em;
    margin: 24px 0 8px;
    color: var(--blue);
  }}
  h3 {{
    font-size: 1.1em;
    margin: 16px 0 6px;
    color: var(--text2);
  }}
  p {{ margin: 8px 0; }}
  ul, ol {{ margin: 8px 0 8px 24px; }}
  li {{ margin: 4px 0; }}
  table {{
    width: 100%;
    border-collapse: collapse;
    margin: 12px 0;
    font-size: 0.9em;
  }}
  th, td {{
    padding: 8px 12px;
    border: 1px solid var(--border);
    text-align: left;
  }}
  th {{
    background: var(--card);
    color: var(--blue);
    font-weight: 600;
  }}
  td {{ background: var(--bg); }}
  tr:hover td {{ background: var(--card); }}
  code {{
    background: var(--card);
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 0.9em;
  }}
  pre {{
    background: var(--card);
    padding: 16px;
    border-radius: 8px;
    overflow-x: auto;
    margin: 12px 0;
    border: 1px solid var(--border);
  }}
  pre code {{ background: none; padding: 0; }}
  blockquote {{
    border-left: 3px solid var(--blue);
    padding: 8px 16px;
    margin: 12px 0;
    color: var(--text2);
    background: var(--card);
    border-radius: 0 4px 4px 0;
  }}
  .positive {{ color: var(--green); }}
  .negative {{ color: var(--red); }}
  .timestamp {{
    text-align: center;
    color: var(--text2);
    font-size: 0.85em;
    margin-top: 40px;
    padding-top: 16px;
    border-top: 1px solid var(--border);
  }}
  strong {{ color: var(--text); }}
  em {{ color: var(--orange); font-style: normal; }}
  hr {{ border: none; border-top: 1px solid var(--border); margin: 20px 0; }}
  @media (max-width: 600px) {{
    body {{ padding: 12px; font-size: 14px; }}
    th, td {{ padding: 6px 8px; font-size: 0.85em; }}
  }}
</style>
</head>
<body>
{content}
<div class="timestamp">Generated at {timestamp} · 📊 交易猎手</div>
</body>
</html>"""


def md_to_html(md: str) -> str:
    """Simple markdown to HTML converter (no external deps)."""
    lines = md.split('\n')
    html_lines = []
    in_table = False
    in_code = False
    in_ul = False
    in_ol = False

    i = 0
    while i < len(lines):
        line = lines[i]

        # Code blocks
        if line.strip().startswith('```'):
            if in_code:
                html_lines.append('</code></pre>')
                in_code = False
            else:
                lang = line.strip()[3:]
                html_lines.append(f'<pre><code class="{lang}">')
                in_code = True
            i += 1
            continue

        if in_code:
            html_lines.append(line.replace('<', '&lt;').replace('>', '&gt;'))
            i += 1
            continue

        # Close lists if needed
        if in_ul and not line.strip().startswith(('- ', '* ', '• ')):
            html_lines.append('</ul>')
            in_ul = False
        if in_ol and not re.match(r'^\d+\. ', line.strip()):
            html_lines.append('</ol>')
            in_ol = False

        # Empty line
        if not line.strip():
            i += 1
            continue

        # Headers
        if line.startswith('# '):
            html_lines.append(f'<h1>{inline_md(line[2:])}</h1>')
        elif line.startswith('## '):
            html_lines.append(f'<h2>{inline_md(line[3:])}</h2>')
        elif line.startswith('### '):
            html_lines.append(f'<h3>{inline_md(line[4:])}</h3>')
        elif line.startswith('---') or line.startswith('***'):
            html_lines.append('<hr>')
        # Blockquote
        elif line.startswith('>'):
            html_lines.append(f'<blockquote>{inline_md(line[1:].strip())}</blockquote>')
        # Table
        elif '|' in line and line.strip().startswith('|'):
            cells = [c.strip() for c in line.strip().strip('|').split('|')]
            if i + 1 < len(lines) and re.match(r'^[\|\s\-:]+$', lines[i + 1].strip()):
                # Header row
                html_lines.append('<table><thead><tr>')
                for c in cells:
                    html_lines.append(f'<th>{inline_md(c)}</th>')
                html_lines.append('</tr></thead><tbody>')
                in_table = True
                i += 2  # skip separator
                continue
            elif in_table:
                html_lines.append('<tr>')
                for c in cells:
                    val = inline_md(c)
                    css = ''
                    if any(x in c for x in ['+', '涨', '🟢']):
                        css = ' class="positive"'
                    elif any(x in c for x in ['跌', '🔴']):
                        css = ' class="negative"'
                    html_lines.append(f'<td{css}>{val}</td>')
                html_lines.append('</tr>')
            else:
                html_lines.append(f'<p>{inline_md(line)}</p>')
        elif in_table and '|' not in line:
            html_lines.append('</tbody></table>')
            in_table = False
            i -= 0  # re-process this line? no, continue below
            html_lines.append(f'<p>{inline_md(line)}</p>')
        # Unordered list
        elif line.strip().startswith(('- ', '* ', '• ')):
            if not in_ul:
                html_lines.append('<ul>')
                in_ul = True
            content = re.sub(r'^[\s]*[-*•]\s+', '', line)
            html_lines.append(f'<li>{inline_md(content)}</li>')
        # Ordered list
        elif re.match(r'^\d+\. ', line.strip()):
            if not in_ol:
                html_lines.append('<ol>')
                in_ol = True
            content = re.sub(r'^\s*\d+\.\s+', '', line)
            html_lines.append(f'<li>{inline_md(content)}</li>')
        # Paragraph
        else:
            html_lines.append(f'<p>{inline_md(line)}</p>')

        i += 1

    # Close open tags
    if in_table:
        html_lines.append('</tbody></table>')
    if in_ul:
        html_lines.append('</ul>')
    if in_ol:
        html_lines.append('</ol>')
    if in_code:
        html_lines.append('</code></pre>')

    return '\n'.join(html_lines)


def inline_md(text: str) -> str:
    """Convert inline markdown: bold, italic, code, links."""
    # Code
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    # Bold
    text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)
    # Italic
    text = re.sub(r'\*([^*]+)\*', r'<em>\1</em>', text)
    # Links
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2" style="color:var(--blue)">\1</a>', text)
    return text


def upload(html: str):
    """Upload HTML to remote server."""
    proc = subprocess.run(
        ["ssh", REMOTE, f"cat > {REMOTE_PATH}"],
        input=html.encode(),
        capture_output=True,
        timeout=15,
    )
    if proc.returncode != 0:
        print(f"Upload error: {proc.stderr.decode()}", file=sys.stderr)
        sys.exit(1)
    print(f"✅ Uploaded to https://blog.balen.wang/report/")


def main():
    date_str = datetime.datetime.now().strftime("%Y-%m-%d")

    # Parse args
    md_content = None
    for arg in sys.argv[1:]:
        if arg.startswith("--date"):
            continue
        elif sys.argv[sys.argv.index(arg) - 1] == "--date":
            date_str = arg
        elif os.path.isfile(arg):
            with open(arg, 'r') as f:
                md_content = f.read()

    if md_content is None:
        if not sys.stdin.isatty():
            md_content = sys.stdin.read()
        else:
            print("Usage: python3 generate_report_html.py [--date YYYY-MM-DD] <file.md>")
            print("       echo 'markdown' | python3 generate_report_html.py")
            sys.exit(1)

    html_body = md_to_html(md_content)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M CST")
    full_html = HTML_TEMPLATE.format(
        date=date_str,
        content=html_body,
        timestamp=timestamp,
    )

    upload(full_html)


if __name__ == "__main__":
    main()
