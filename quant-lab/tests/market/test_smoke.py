"""M-01 骨架冒烟：包可 import、钉死版本、凭据边界。"""
from __future__ import annotations

import importlib
import pathlib
import re
import sys


def test_market_package_importable():
    import quant_lab.market as m

    assert m.EXECUTION_CONTRACT_VERSION.startswith("g2-exec-")
    for name in ["vision", "partition_check", "asof", "contract", "kernel_a", "nautilus_adapter", "execution"]:
        importlib.import_module(f"quant_lab.market.{name}")


def test_pinned_nautilus_version():
    import nautilus_trader

    assert nautilus_trader.__version__ == "1.227.0"


def test_no_services_import():
    """铁律：研究层不 import services/*。"""
    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "quant_lab" / "market"
    for f in root.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from|import)\s+services\b", text, re.M), f
    assert not any(k == "services" or k.startswith("services.") for k in sys.modules)
