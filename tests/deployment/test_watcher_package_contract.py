"""Guard the watcher image's local JavaScript dependency closure and secrets wiring."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WATCHER = ROOT / "bridge/services/telegram-watcher"
COMPOSE = ROOT / "bridge/docker-compose.yml"
BUILDER = ROOT / "scripts/build_immutable_watcher_image.py"
REQUIRE = re.compile(r"\brequire\s*\(\s*(['\"])(\.[^'\"]+)\1\s*\)")
CREDENTIALS = {
    f"WATCHER_{identity}_TOKEN{suffix}"
    for identity in ("GATEWAY", "SNAPSHOT", "BROWSER_PROXY")
    for suffix in ("", "_PREVIOUS")
}


def _runtime_paths() -> set[str]:
    spec = importlib.util.spec_from_file_location("watcher_image_builder", BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return set(module.WATCHER_RUNTIME_RELATIVE_PATHS)


def _resolve_local_require(source: Path, name: str) -> Path:
    base = source.parent / name
    candidates = (base, Path(f"{base}.js"), base / "index.js")
    for candidate in candidates:
        if candidate.is_file():
            resolved = candidate.resolve()
            assert resolved.is_relative_to(WATCHER.resolve()), (
                f"local require escapes watcher root: {source}: {name}"
            )
            return resolved
    raise AssertionError(f"unresolved local require: {source}: {name}")


def test_watcher_runtime_require_closure_is_packaged() -> None:
    whitelist = _runtime_paths()
    missing_sources = sorted(path for path in whitelist if not (WATCHER / path).is_file())
    assert not missing_sources, f"nonexistent watcher whitelist files: {missing_sources}"

    # The server, monitor, and other shipped JS entry points may each load modules.
    pending = [WATCHER / path for path in whitelist if path.endswith(".js")]
    pending.extend((WATCHER / "server.js", WATCHER / "price-monitor.js"))
    visited: set[Path] = set()
    while pending:
        source = pending.pop().resolve()
        if source in visited:
            continue
        visited.add(source)
        for match in REQUIRE.finditer(source.read_text(encoding="utf-8")):
            pending.append(_resolve_local_require(source, match.group(2)))

    required = {str(path.relative_to(WATCHER.resolve())) for path in visited}
    missing = sorted(required - whitelist)
    assert not missing, f"local require missing from watcher image whitelist: {missing}"


def test_watcher_compose_uses_private_credential_env_file() -> None:
    # The system Python used by the route generator provides PyYAML; the
    # deployment pytest venv deliberately does not include it.
    parser = (
        "import json,sys,yaml; "
        "print(json.dumps(yaml.safe_load(open(sys.argv[1], encoding='utf-8'))))"
    )
    parsed = subprocess.run(
        ["python3", "-c", parser, str(COMPOSE)],
        check=True,
        capture_output=True,
        text=True,
    )
    watcher = json.loads(parsed.stdout)["services"]["watcher"]
    env_files = watcher["env_file"]
    if isinstance(env_files, str):
        env_files = [env_files]
    assert "/srv/trader-secrets/watcher-gateway.env" in env_files

    environment = watcher.get("environment", {})
    if isinstance(environment, list):
        environment = {entry.split("=", 1)[0]: entry for entry in environment}
    assert CREDENTIALS.isdisjoint(environment)
    assert not any(
        re.search(r"\$\{?WATCHER_(?:GATEWAY|SNAPSHOT|BROWSER_PROXY)_TOKEN", str(value))
        for value in environment.values()
    )
