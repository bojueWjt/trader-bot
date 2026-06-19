from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
BRIDGE_APP = REPO_ROOT / "bridge" / "apps" / "api" / "app"
WRITE_ROUTE_RE = re.compile(r"@router\.(post|put|patch|delete)\(\s*['\"][^'\"]*watcher", re.IGNORECASE)


def test_watcher_anonymous_write_route_does_not_exist() -> None:
    source = "\n".join(path.read_text() for path in sorted(BRIDGE_APP.rglob("*.py")))

    assert WRITE_ROUTE_RE.search(source) is None
