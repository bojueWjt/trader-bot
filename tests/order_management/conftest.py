from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONTROL_PLANE = ROOT / "services" / "control-plane"
CONTROL_PLANE_RISK = CONTROL_PLANE / "risk"
EXECUTION_DOMAIN = ROOT / "packages" / "execution-domain"
NAUTILUS_COMMANDS = ROOT / "services" / "nautilus-node" / "commands"
_NAUTILUS_COMMANDS_ALIAS = "_ledger_test_nautilus_commands"

for _path in (CONTROL_PLANE, CONTROL_PLANE_RISK, EXECUTION_DOMAIN):
    _p = str(_path)
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _install_nautilus_commands_alias() -> None:
    if _NAUTILUS_COMMANDS_ALIAS in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        _NAUTILUS_COMMANDS_ALIAS,
        NAUTILUS_COMMANDS / "__init__.py",
        submodule_search_locations=[str(NAUTILUS_COMMANDS)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_NAUTILUS_COMMANDS_ALIAS] = module
    spec.loader.exec_module(module)


_install_nautilus_commands_alias()
