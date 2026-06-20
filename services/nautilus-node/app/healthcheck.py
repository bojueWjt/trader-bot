from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.error import URLError
from urllib.request import urlopen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("check", choices=("liveness", "readiness"))
    parser.add_argument(
        "--url",
        default=os.environ.get("NAUTILUS_HEALTH_URL", "http://127.0.0.1:8081"),
    )
    args = parser.parse_args(argv)
    path = "/live" if args.check == "liveness" else "/ready"
    try:
        with urlopen(f"{args.url.rstrip('/')}{path}", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
            status = response.status
    except URLError as exc:
        print(f"{args.check} failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"{args.check} failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(payload, sort_keys=True))
    return 0 if status == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
