"""stdlib HTTP server; explicit configuration, loopback binding, GET-only routes."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

from .data import Dashboard, dumps

STATIC = Path(__file__).parent / "static"


def make_server(dashboard: Dashboard, host: str = "127.0.0.1", port: int = 8765, *, bind_and_activate: bool = True) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def send(self, status: int, body: bytes, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' https://unpkg.com; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def json(self, status: int, value):
            self.send(status, dumps(value).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self):
            try:
                url = urlsplit(self.path)
                query = parse_qs(url.query, keep_blank_values=True)
                allowed = {
                    "/": set(), "/channel": {"channel"}, "/trade": {"channel", "episode", "variant"},
                    "/api/health": set(), "/api/overview": set(), "/api/channel": {"channel"},
                    "/api/detail": {"channel", "episode", "variant", "interval"},
                    "/static/app.js": set(), "/static/style.css": set(),
                }
                if url.path not in allowed or not set(query).issubset(allowed[url.path]) or any(len(v) != 1 for v in query.values()):
                    raise LookupError("未知接口或参数")

                def arg(name):
                    values = query.get(name)
                    if not values or not values[0]:
                        raise LookupError("缺少参数")
                    return values[0]

                with dashboard.lock:
                    if url.path == "/api/health":
                        self.json(200, {"service": "quant-lab-dashboard", "config_id": dashboard.config_id})
                    elif url.path == "/api/overview":
                        self.json(200, dashboard.overview())
                    elif url.path == "/api/channel":
                        self.json(200, dashboard.trades(arg("channel")))
                    elif url.path == "/api/detail":
                        interval = query.get("interval", [None])[0]
                        self.json(200, dashboard.detail(arg("channel"), arg("variant"), arg("episode"), interval))
                    elif url.path in {"/", "/channel", "/trade"}:
                        if url.path != "/":
                            dashboard.channel(arg("channel"))
                        if url.path == "/trade":
                            trades = dashboard.trades(arg("channel"))["trades"]
                            variant = arg("variant")
                            if variant not in dashboard.variants() or not any(row["episode_id"] == arg("episode") and variant in row["variants"] for row in trades):
                                raise LookupError("未知单笔或口径")
                        self.send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
                    else:
                        file = STATIC / url.path.rsplit("/", 1)[1]
                        kind = "text/javascript" if file.suffix == ".js" else "text/css"
                        self.send(200, file.read_bytes(), kind + "; charset=utf-8")
            except LookupError as error:
                self.json(404, {"error": str(error)})
            except (ValueError, OSError) as error:
                self.json(409, {"error": str(error)})
            except Exception as error:
                print(f"dashboard request failed: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
                self.json(500, {"error": "数据读取失败，请检查服务日志"})

        def readonly(self):
            self.json(405, {"error": "只读服务，仅支持 GET"})

        do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = readonly

        def log_message(self, format, *args):
            # Do not put episode IDs or query text into access logs.
            print(f"dashboard HTTP {args[1] if len(args) > 1 else ''}", file=sys.stderr, flush=True)

    server = ThreadingHTTPServer((host, port), Handler, bind_and_activate=bind_and_activate)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="quant-lab 只读回测看板（数据不离开研究机）")
    parser.add_argument("--config", required=True, help="研究机上的 JSON 配置文件")
    parser.add_argument("--bind", default="127.0.0.1", help="默认仅 loopback；其他地址需显式指定")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        loopback = args.bind == "localhost" or ipaddress.ip_address(args.bind).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        print("警告：看板包含消息原文与交易数据，当前显式绑定到非本地地址；服务没有认证。", file=sys.stderr, flush=True)
    dashboard = Dashboard(args.config)
    server = make_server(dashboard, args.bind, args.port)
    print(f"回测看板 http://{args.bind}:{server.server_port} （GET 只读）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
