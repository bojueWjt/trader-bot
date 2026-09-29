#!/usr/bin/env python3
"""Run the control-plane app that hosts the watcher gateway, loopback only.

Started by backend.py with a scrubbed environment. The state directory is
passed on the command line so ``down`` can recognise its own processes.
The bind host is fixed to 127.0.0.1 and is not configurable on purpose.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

HOST = "127.0.0.1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--app-dir", required=True, type=Path)
    parser.add_argument("--app", default="read_api:app")
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args(argv)
    if not args.state_dir.joinpath(".wac-local-backend").is_file():
        print("state dir marker missing; refusing to start", file=sys.stderr)
        return 2
    import uvicorn

    sys.path.insert(0, str(args.app_dir.resolve()))
    module_name, _, attribute = args.app.partition(":")
    app = getattr(importlib.import_module(module_name), attribute or "app")
    uvicorn.run(app, host=HOST, port=args.port, workers=1, log_level="info", proxy_headers=False, server_header=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
