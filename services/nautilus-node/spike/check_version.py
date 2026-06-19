#!/usr/bin/env python3
from __future__ import annotations

import platform
import sys


EXPECTED_VERSION = "1.227.0"


def main() -> int:
    print(f"python={sys.version.split()[0]}")
    print(f"platform={platform.platform()}")
    print(f"machine={platform.machine()}")

    try:
        import nautilus_trader
    except Exception as exc:  # pragma: no cover - target-host diagnostic
        print(f"nautilus_trader_import=FAILED: {exc!r}")
        return 1

    version = getattr(nautilus_trader, "__version__", None)
    print(f"nautilus_trader_version={version}")
    if version != EXPECTED_VERSION:
        print(f"EXPECTED {EXPECTED_VERSION}, got {version}")
        return 1

    print("version_check=OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
