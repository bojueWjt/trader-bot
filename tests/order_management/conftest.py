from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONTROL_PLANE = ROOT / "services" / "control-plane"
CONTROL_PLANE_RISK = CONTROL_PLANE / "risk"
EXECUTION_DOMAIN = ROOT / "packages" / "execution-domain"

for _path in (CONTROL_PLANE, CONTROL_PLANE_RISK, EXECUTION_DOMAIN):
    _p = str(_path)
    if _p not in sys.path:
        sys.path.insert(0, _p)
