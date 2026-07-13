#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


DEFAULT_ENDPOINT = "http://127.0.0.1:8090"


def die(message: str, code: int = 1) -> int:
    print(message, file=sys.stderr)
    return code


def token_from_env() -> str | None:
    return os.getenv("REPORT_TOKEN") or None


def request_json(
    url: str,
    method: str,
    body: bytes,
    token: str | None,
    content_type: str = "application/json",
) -> dict:
    headers = {"Content-Type": content_type}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"request failed: {exc.reason}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"server returned non-JSON response: {raw[:300]}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("server returned unexpected JSON")
    return parsed


def load_publish_payload(args: argparse.Namespace) -> bytes:
    if args.json:
        text = Path(args.json).read_text(encoding="utf-8")
    else:
        text = sys.stdin.read()
    if not text.strip():
        raise RuntimeError("publish requires --json FILE or JSON on stdin")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON payload: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("payload must be a JSON object")
    payload["type"] = args.type
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def cmd_publish(args: argparse.Namespace) -> int:
    try:
        body = load_publish_payload(args)
        result = request_json(f"{args.endpoint.rstrip('/')}/reports", "POST", body, token_from_env())
    except Exception as exc:
        return die(str(exc))
    url = result.get("url")
    if not url:
        return die(f"server response did not include url: {result}")
    print(url)
    return 0


def multipart_body(path: Path, field_name: str = "file") -> tuple[bytes, str]:
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    boundary = "----v3report" + os.urandom(12).hex()
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; filename="{path.name}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    return head + path.read_bytes() + tail, f"multipart/form-data; boundary={boundary}"


def cmd_upload(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists() or not path.is_file():
        return die(f"file not found: {path}", 2)
    try:
        body, content_type = multipart_body(path)
        result = request_json(
            f"{args.endpoint.rstrip('/')}/reports/assets",
            "POST",
            body,
            token_from_env(),
            content_type=content_type,
        )
    except Exception as exc:
        return die(str(exc))
    url = result.get("url")
    if not url:
        return die(f"server response did not include url: {result}")
    print(url)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish Hermes V3 daily/weekly reports")
    sub = parser.add_subparsers(dest="cmd", required=True)

    publish = sub.add_parser("publish", help="publish a daily or weekly report JSON")
    publish.add_argument("--type", choices=("daily", "weekly"), required=True)
    publish.add_argument("--json", help="JSON file path; omit to read stdin")
    publish.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    publish.set_defaults(fn=cmd_publish)

    upload = sub.add_parser("upload", help="upload an image asset")
    upload.add_argument("path", help="png/jpeg/webp image path")
    upload.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    upload.set_defaults(fn=cmd_upload)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
